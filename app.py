"""
ChamaPay Kenya - Production Chama platform
Monthly subscription per Chama. Unpaid = locked until payment.
"""
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
from functools import wraps
import os
import random

from config import Config, PLANS

app = Flask(__name__)
app.config.from_object(Config)
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'warning'

# ==================== MODELS ====================

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(15), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_platform_owner = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Chama(db.Model):
    __tablename__ = 'chamas'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, default='')
    contribution_amount = db.Column(db.Float, default=1000.0)
    contribution_day = db.Column(db.Integer, default=5)
    loan_interest_rate = db.Column(db.Float, default=5.0)
    max_loan_multiplier = db.Column(db.Float, default=3.0)
    currency = db.Column(db.String(10), default='KES')
    # Subscription
    plan = db.Column(db.String(20), default='basic')  # basic, standard, premium
    subscription_status = db.Column(db.String(20), default='trial')  # trial, active, locked
    trial_ends_at = db.Column(db.DateTime)
    subscription_ends_at = db.Column(db.DateTime)
    last_reminder_sent = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    def monthly_price(self):
        return PLANS.get(self.plan, PLANS['basic'])['price']

    def plan_name(self):
        return PLANS.get(self.plan, PLANS['basic'])['name']

    def days_until_expiry(self):
        now = datetime.utcnow()
        end = None
        if self.subscription_status == 'trial' and self.trial_ends_at:
            end = self.trial_ends_at
        elif self.subscription_ends_at:
            end = self.subscription_ends_at
        if not end:
            return None
        return (end.date() - now.date()).days

    def refresh_lock_status(self):
        """Lock if trial/subscription expired. Returns True if locked."""
        now = datetime.utcnow()
        if self.subscription_status == 'locked':
            return True
        if self.subscription_status == 'trial' and self.trial_ends_at and now > self.trial_ends_at:
            self.subscription_status = 'locked'
            db.session.commit()
            return True
        if self.subscription_status == 'active' and self.subscription_ends_at and now > self.subscription_ends_at:
            self.subscription_status = 'locked'
            db.session.commit()
            return True
        return self.subscription_status == 'locked'

    def is_locked(self):
        return self.refresh_lock_status()

    def unlock_until(self, months=1):
        """Unlock after successful payment."""
        now = datetime.utcnow()
        base = self.subscription_ends_at if (self.subscription_ends_at and self.subscription_ends_at > now) else now
        self.subscription_ends_at = base + relativedelta(months=months)
        self.subscription_status = 'active'
        db.session.commit()


class Membership(db.Model):
    __tablename__ = 'memberships'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    chama_id = db.Column(db.Integer, db.ForeignKey('chamas.id'), nullable=False)
    role = db.Column(db.String(20), default='member')  # chairperson, treasurer, secretary, member
    total_savings = db.Column(db.Float, default=0.0)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    user = db.relationship('User', backref='memberships')
    chama = db.relationship('Chama', backref='memberships')


class Contribution(db.Model):
    __tablename__ = 'contributions'
    id = db.Column(db.Integer, primary_key=True)
    chama_id = db.Column(db.Integer, db.ForeignKey('chamas.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(30), default='Cash')
    mpesa_code = db.Column(db.String(30))
    month = db.Column(db.String(7))
    notes = db.Column(db.String(200))
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])


class Loan(db.Model):
    __tablename__ = 'loans'
    id = db.Column(db.Integer, primary_key=True)
    chama_id = db.Column(db.Integer, db.ForeignKey('chamas.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    interest_rate = db.Column(db.Float, nullable=False)
    total_repayable = db.Column(db.Float, nullable=False)
    amount_paid = db.Column(db.Float, default=0.0)
    purpose = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pending')  # pending, active, repaid, rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.Date)

    user = db.relationship('User', foreign_keys=[user_id])


class LoanRepayment(db.Model):
    __tablename__ = 'loan_repayments'
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loans.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(30), default='Cash')
    mpesa_code = db.Column(db.String(30))
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SubscriptionPayment(db.Model):
    """Monthly platform fee paid by a Chama."""
    __tablename__ = 'subscription_payments'
    id = db.Column(db.Integer, primary_key=True)
    chama_id = db.Column(db.Integer, db.ForeignKey('chamas.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    months = db.Column(db.Integer, default=1)
    phone = db.Column(db.String(15))
    mpesa_receipt = db.Column(db.String(50))
    status = db.Column(db.String(20), default='pending')  # pending, completed, failed
    paid_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    chama = db.relationship('Chama', backref='subscription_payments')




class SMSLog(db.Model):
    """Log of SMS reminders sent (simulated or real)."""
    __tablename__ = 'sms_logs'
    id = db.Column(db.Integer, primary_key=True)
    chama_id = db.Column(db.Integer, db.ForeignKey('chamas.id'), nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    message = db.Column(db.Text, nullable=False)
    purpose = db.Column(db.String(40), default='expiry_reminder')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ==================== HELPERS ====================

def normalize_phone(phone):
    phone = (phone or '').strip().replace(' ', '').replace('-', '').replace('+', '')
    if phone.startswith('254') and len(phone) >= 12:
        phone = '0' + phone[3:]
    if len(phone) == 9 and phone[0] in '17':
        phone = '0' + phone
    return phone




def send_sms(phone, message, chama_id=None, purpose='general'):
    """Send SMS (simulated by default)."""
    phone = normalize_phone(phone)
    print(f'[SMS] to {phone}: {message[:80]}...')
    if chama_id:
        db.session.add(SMSLog(chama_id=chama_id, phone=phone, message=message, purpose=purpose))
        db.session.commit()
    return True


def send_expiry_reminders():
    """Send SMS to chama admins N days before lock. Call on dashboard load / cron."""
    days = app.config.get('REMINDER_DAYS_BEFORE', 3)
    now = datetime.utcnow()
    sent = 0
    for chama in Chama.query.filter(Chama.subscription_status.in_(['trial', 'active'])).all():
        d = chama.days_until_expiry()
        if d is None or d < 0 or d > days:
            continue
        # avoid spam: one reminder per day window
        if chama.last_reminder_sent and (now - chama.last_reminder_sent).days < 1:
            continue
        admins = Membership.query.filter(
            Membership.chama_id == chama.id,
            Membership.is_active == True,
            Membership.role.in_(['chairperson', 'treasurer'])
        ).all()
        price = chama.monthly_price()
        msg = (
            f"ChamaPay: {chama.name} subscription expires in {d} day(s). "
            f"Pay KES {price:,.0f} to avoid lock. Open app to renew."
        )
        for m in admins:
            send_sms(m.user.phone, msg, chama_id=chama.id, purpose='expiry_reminder')
            sent += 1
        chama.last_reminder_sent = now
        db.session.commit()
    return sent

def get_membership(user_id, chama_id):
    return Membership.query.filter_by(user_id=user_id, chama_id=chama_id, is_active=True).first()


def is_chama_admin(user, chama_id):
    m = get_membership(user.id, chama_id)
    return m and m.role in ('chairperson', 'treasurer')


def require_unlocked(f):
    """Block write actions if Chama subscription is locked."""
    @wraps(f)
    def wrapped(chama_id, *args, **kwargs):
        chama = db.session.get(Chama, chama_id)
        if not chama:
            flash('Chama not found.', 'danger')
            return redirect(url_for('dashboard'))
        if chama.is_locked() and not current_user.is_platform_owner:
            flash('This Chama is locked. Please renew the monthly subscription to continue.', 'warning')
            return redirect(url_for('subscription_pay', chama_id=chama_id))
        return f(chama_id, *args, **kwargs)
    return wrapped


def user_chamas(user):
    return Chama.query.join(Membership).filter(
        Membership.user_id == user.id,
        Membership.is_active == True,
        Chama.is_active == True
    ).all()

# ==================== AUTH ====================

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('index.html', plans=PLANS, trial=app.config['TRIAL_DAYS'])


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = normalize_phone(request.form.get('phone', ''))
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not name or not phone or not password:
            flash('All fields are required.', 'danger')
            return render_template('register.html')
        if len(phone) < 10:
            flash('Enter a valid phone number e.g. 0712345678', 'danger')
            return render_template('register.html')
        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('register.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return render_template('register.html')
        if User.query.filter_by(phone=phone).first():
            flash('Phone already registered. Please login.', 'warning')
            return redirect(url_for('login'))
        user = User(name=name, phone=phone)
        user.set_password(password)
        # First matching owner phone becomes platform owner
        if phone == normalize_phone(app.config.get('OWNER_PHONE', '')):
            user.is_platform_owner = True
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        flash('Account created successfully. Welcome!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        phone = normalize_phone(request.form.get('phone', ''))
        password = request.form.get('password', '')
        user = User.query.filter_by(phone=phone).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid phone or password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

# ==================== DASHBOARD ====================

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        send_expiry_reminders()
    except Exception as e:
        print('Reminder error:', e)
    chamas = user_chamas(current_user)
    for c in chamas:
        c.refresh_lock_status()
    return render_template('dashboard.html', chamas=chamas, plans=PLANS)


@app.route('/chama/create', methods=['GET', 'POST'])
@login_required
def create_chama():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        amount = float(request.form.get('contribution_amount') or 1000)
        if not name:
            flash('Chama name is required.', 'danger')
            return render_template('create_chama.html', trial=app.config['TRIAL_DAYS'], plans=PLANS)
        trial_days = app.config['TRIAL_DAYS']
        plan = request.form.get('plan', 'basic')
        if plan not in PLANS:
            plan = 'basic'
        chama = Chama(
            name=name,
            description=description,
            contribution_amount=amount,
            contribution_day=int(request.form.get('contribution_day') or 5),
            loan_interest_rate=float(request.form.get('loan_interest_rate') or 5),
            plan=plan,
            created_by=current_user.id,
            subscription_status='trial',
            trial_ends_at=datetime.utcnow() + timedelta(days=trial_days),
        )
        db.session.add(chama)
        db.session.flush()
        db.session.add(Membership(
            user_id=current_user.id,
            chama_id=chama.id,
            role='chairperson',
            total_savings=0,
        ))
        db.session.commit()
        flash(f'Chama created! You have {trial_days} days free trial.', 'success')
        return redirect(url_for('chama_home', chama_id=chama.id))
    return render_template('create_chama.html', trial=app.config['TRIAL_DAYS'], plans=PLANS)


@app.route('/chama/<int:chama_id>')
@login_required
def chama_home(chama_id):
    chama = db.session.get(Chama, chama_id) or abort_not_found()
    membership = get_membership(current_user.id, chama_id)
    if not membership and not current_user.is_platform_owner:
        flash('You are not a member of this Chama.', 'danger')
        return redirect(url_for('dashboard'))
    locked = chama.is_locked()
    members = Membership.query.filter_by(chama_id=chama_id, is_active=True).all()
    recent = Contribution.query.filter_by(chama_id=chama_id).order_by(Contribution.created_at.desc()).limit(8).all()
    loans = Loan.query.filter(Loan.chama_id == chama_id, Loan.status.in_(['pending', 'active'])).all()
    is_admin = is_chama_admin(current_user, chama_id) or current_user.is_platform_owner
    total_savings = sum(m.total_savings for m in members)
    return render_template(
        'chama_home.html',
        chama=chama, membership=membership, members=members,
        recent=recent, loans=loans, is_admin=is_admin,
        locked=locked, total_savings=total_savings,
        plans=PLANS,
    )


def abort_not_found():
    flash('Not found.', 'danger')
    return redirect(url_for('dashboard'))

# ==================== SUBSCRIPTION (YOUR REVENUE) ====================

@app.route('/chama/<int:chama_id>/subscribe', methods=['GET', 'POST'])
@login_required
def subscription_pay(chama_id):
    chama = db.session.get(Chama, chama_id)
    if not chama:
        return abort_not_found()
    membership = get_membership(current_user.id, chama_id)
    if not membership and not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        plan = request.form.get('plan') or chama.plan or 'basic'
        if plan not in PLANS:
            plan = 'basic'
        months = int(request.form.get('months') or 1)
        months = max(1, min(months, 12))
        price = PLANS[plan]['price']
        amount = price * months
        phone = normalize_phone(request.form.get('phone') or current_user.phone)

        payment = SubscriptionPayment(
            chama_id=chama.id,
            amount=amount,
            months=months,
            phone=phone,
            paid_by=current_user.id,
            status='pending',
        )
        db.session.add(payment)
        db.session.commit()

        if app.config.get('SIMULATE_PAYMENTS', True) or not app.config.get('MPESA_CONSUMER_KEY'):
            payment.status = 'completed'
            payment.mpesa_receipt = f'SIM{random.randint(100000,999999)}'
            payment.completed_at = datetime.utcnow()
            chama.plan = plan
            chama.unlock_until(months)
            db.session.commit()
            # Confirm SMS to payer
            send_sms(
                phone,
                f"ChamaPay: Payment KES {amount:,.0f} received for {chama.name}. Unlocked {months} month(s) on {PLANS[plan]['name']} plan. Asante!",
                chama_id=chama.id,
                purpose='payment_confirm',
            )
            flash(f'Payment of KES {amount:,.0f} received. Chama unlocked for {months} month(s)!', 'success')
            return redirect(url_for('chama_home', chama_id=chama.id))
        else:
            flash('M-Pesa prompt sent. Complete payment on your phone. Chama unlocks after payment.', 'info')
            return redirect(url_for('subscription_pay', chama_id=chama.id))

    return render_template(
        'subscribe.html',
        chama=chama,
        plans=PLANS,
        current_plan=chama.plan or 'basic',
        locked=chama.is_locked(),
    )


@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """Safaricom STK callback — unlock Chama on successful pay."""
    try:
        data = request.get_json(force=True) or {}
        body = data.get('Body', {}).get('stkCallback', {})
        result_code = body.get('ResultCode')
        if result_code == 0:
            items = {i['Name']: i.get('Value') for i in body.get('CallbackMetadata', {}).get('Item', [])}
            receipt = items.get('MpesaReceiptNumber')
            amount = float(items.get('Amount') or 0)
            # Match latest pending payment of this amount
            payment = SubscriptionPayment.query.filter_by(status='pending', amount=amount).order_by(
                SubscriptionPayment.created_at.desc()
            ).first()
            if payment:
                payment.status = 'completed'
                payment.mpesa_receipt = str(receipt or '')
                payment.completed_at = datetime.utcnow()
                chama = db.session.get(Chama, payment.chama_id)
                if chama:
                    chama.unlock_until(payment.months or 1)
                db.session.commit()
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})
    except Exception as e:
        print('Callback error:', e)
        return jsonify({'ResultCode': 1, 'ResultDesc': str(e)}), 500

# ==================== MEMBERS ====================

@app.route('/chama/<int:chama_id>/members')
@login_required
def members(chama_id):
    chama = db.session.get(Chama, chama_id)
    if not get_membership(current_user.id, chama_id) and not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    members_list = Membership.query.filter_by(chama_id=chama_id, is_active=True).all()
    is_admin = is_chama_admin(current_user, chama_id)
    return render_template('members.html', chama=chama, members=members_list, is_admin=is_admin, locked=chama.is_locked())


@app.route('/chama/<int:chama_id>/add_member', methods=['POST'])
@login_required
@require_unlocked
def add_member(chama_id):
    if not is_chama_admin(current_user, chama_id):
        flash('Only chairperson/treasurer can add members.', 'danger')
        return redirect(url_for('members', chama_id=chama_id))
    phone = normalize_phone(request.form.get('phone', ''))
    role = request.form.get('role', 'member')
    user = User.query.filter_by(phone=phone).first()
    if not user:
        flash('User not found. They must register on the app first.', 'danger')
        return redirect(url_for('members', chama_id=chama_id))
    if get_membership(user.id, chama_id):
        flash('Already a member.', 'warning')
        return redirect(url_for('members', chama_id=chama_id))
    db.session.add(Membership(user_id=user.id, chama_id=chama_id, role=role))
    db.session.commit()
    flash(f'{user.name} added.', 'success')
    return redirect(url_for('members', chama_id=chama_id))

# ==================== CONTRIBUTIONS ====================

@app.route('/chama/<int:chama_id>/contributions')
@login_required
def contributions(chama_id):
    chama = db.session.get(Chama, chama_id)
    if not get_membership(current_user.id, chama_id) and not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    rows = Contribution.query.filter_by(chama_id=chama_id).order_by(Contribution.created_at.desc()).all()
    members_list = Membership.query.filter_by(chama_id=chama_id, is_active=True).all()
    return render_template(
        'contributions.html',
        chama=chama, contribs=rows, members=members_list,
        is_admin=is_chama_admin(current_user, chama_id),
        locked=chama.is_locked(),
    )


@app.route('/chama/<int:chama_id>/add_contribution', methods=['POST'])
@login_required
@require_unlocked
def add_contribution(chama_id):
    if not is_chama_admin(current_user, chama_id):
        flash('Only admin can record contributions.', 'danger')
        return redirect(url_for('contributions', chama_id=chama_id))
    user_id = int(request.form.get('user_id'))
    amount = float(request.form.get('amount'))
    method = request.form.get('payment_method', 'Cash')
    mpesa = request.form.get('mpesa_code', '')
    today = date.today()
    c = Contribution(
        chama_id=chama_id, user_id=user_id, amount=amount,
        payment_method=method, mpesa_code=mpesa or None,
        month=today.strftime('%Y-%m'), recorded_by=current_user.id,
    )
    db.session.add(c)
    m = get_membership(user_id, chama_id)
    if m:
        m.total_savings += amount
    db.session.commit()
    flash('Contribution recorded.', 'success')
    return redirect(url_for('contributions', chama_id=chama_id))

# ==================== LOANS ====================

@app.route('/chama/<int:chama_id>/loans')
@login_required
def loans(chama_id):
    chama = db.session.get(Chama, chama_id)
    if not get_membership(current_user.id, chama_id) and not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    rows = Loan.query.filter_by(chama_id=chama_id).order_by(Loan.created_at.desc()).all()
    membership = get_membership(current_user.id, chama_id)
    return render_template(
        'loans.html', chama=chama, loans=rows,
        membership=membership, is_admin=is_chama_admin(current_user, chama_id),
        locked=chama.is_locked(),
    )


@app.route('/chama/<int:chama_id>/apply_loan', methods=['POST'])
@login_required
@require_unlocked
def apply_loan(chama_id):
    membership = get_membership(current_user.id, chama_id)
    if not membership:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    chama = db.session.get(Chama, chama_id)
    amount = float(request.form.get('amount'))
    purpose = request.form.get('purpose', '')
    max_loan = membership.total_savings * chama.max_loan_multiplier
    if amount <= 0 or amount > max_loan:
        flash(f'Max loan is KES {max_loan:,.0f} based on your savings.', 'danger')
        return redirect(url_for('loans', chama_id=chama_id))
    total = amount * (1 + chama.loan_interest_rate / 100)
    loan = Loan(
        chama_id=chama_id, user_id=current_user.id, amount=amount,
        interest_rate=chama.loan_interest_rate, total_repayable=total,
        purpose=purpose, status='pending',
        due_date=date.today() + relativedelta(months=3),
    )
    db.session.add(loan)
    db.session.commit()
    flash('Loan application submitted.', 'success')
    return redirect(url_for('loans', chama_id=chama_id))


@app.route('/loan/<int:loan_id>/decide', methods=['POST'])
@login_required
def decide_loan(loan_id):
    loan = db.session.get(Loan, loan_id)
    if not loan or not is_chama_admin(current_user, loan.chama_id):
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    if db.session.get(Chama, loan.chama_id).is_locked():
        flash('Chama is locked. Renew subscription first.', 'warning')
        return redirect(url_for('subscription_pay', chama_id=loan.chama_id))
    action = request.form.get('action')
    if action == 'approve':
        loan.status = 'active'
        flash('Loan approved.', 'success')
    else:
        loan.status = 'rejected'
        flash('Loan rejected.', 'info')
    db.session.commit()
    return redirect(url_for('loans', chama_id=loan.chama_id))


@app.route('/loan/<int:loan_id>/repay', methods=['POST'])
@login_required
def repay_loan(loan_id):
    loan = db.session.get(Loan, loan_id)
    if not loan:
        return redirect(url_for('dashboard'))
    if db.session.get(Chama, loan.chama_id).is_locked():
        flash('Chama is locked. Renew subscription first.', 'warning')
        return redirect(url_for('subscription_pay', chama_id=loan.chama_id))
    if not is_chama_admin(current_user, loan.chama_id) and loan.user_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    amount = float(request.form.get('amount'))
    remaining = loan.total_repayable - loan.amount_paid
    amount = min(amount, remaining)
    if amount <= 0:
        flash('Invalid amount.', 'danger')
        return redirect(url_for('loans', chama_id=loan.chama_id))
    db.session.add(LoanRepayment(
        loan_id=loan.id, amount=amount,
        payment_method=request.form.get('payment_method', 'Cash'),
        mpesa_code=request.form.get('mpesa_code') or None,
        recorded_by=current_user.id,
    ))
    loan.amount_paid += amount
    if loan.amount_paid >= loan.total_repayable:
        loan.status = 'repaid'
    db.session.commit()
    flash('Repayment recorded.', 'success')
    return redirect(url_for('loans', chama_id=loan.chama_id))

# ==================== PLATFORM OWNER ====================

@app.route('/owner')
@login_required
def owner_dashboard():
    if not current_user.is_platform_owner:
        flash('Owner access only.', 'danger')
        return redirect(url_for('dashboard'))
    chamas = Chama.query.order_by(Chama.created_at.desc()).all()
    for c in chamas:
        c.refresh_lock_status()
    payments = SubscriptionPayment.query.filter_by(status='completed').order_by(
        SubscriptionPayment.completed_at.desc()
    ).limit(50).all()
    revenue = sum(p.amount for p in SubscriptionPayment.query.filter_by(status='completed').all())
    locked_count = sum(1 for c in chamas if c.subscription_status == 'locked')
    active_count = sum(1 for c in chamas if c.subscription_status in ('active', 'trial'))
    return render_template(
        'owner.html',
        chamas=chamas, payments=payments, revenue=revenue,
        locked_count=locked_count, active_count=active_count,
        plans=PLANS,
    )


@app.route('/owner/chama/<int:chama_id>/unlock', methods=['POST'])
@login_required
def owner_unlock(chama_id):
    if not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    chama = db.session.get(Chama, chama_id)
    months = int(request.form.get('months') or 1)
    chama.unlock_until(months)
    flash(f'{chama.name} unlocked for {months} month(s).', 'success')
    return redirect(url_for('owner_dashboard'))


@app.route('/owner/chama/<int:chama_id>/lock', methods=['POST'])
@login_required
def owner_lock(chama_id):
    if not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    chama = db.session.get(Chama, chama_id)
    chama.subscription_status = 'locked'
    db.session.commit()
    flash(f'{chama.name} locked.', 'warning')
    return redirect(url_for('owner_dashboard'))

# ==================== INIT ====================

def init_db():
    db.create_all()
    owner_phone = normalize_phone(app.config.get('OWNER_PHONE', '0700000000'))
    if not User.query.filter_by(phone=owner_phone).first():
        owner = User(name='Platform Owner', phone=owner_phone, is_platform_owner=True)
        owner.set_password('owner123')
        db.session.add(owner)
        db.session.commit()
        print(f'Owner account created: {owner_phone} / owner123')


with app.app_context():
    try:
        init_db()
    except Exception as e:
        print('DB init:', e)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
