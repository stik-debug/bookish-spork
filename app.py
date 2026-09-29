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
import secrets
import hmac
import time
from urllib.parse import urlparse

from config import Config, PLANS

app = Flask(__name__)
app.config.from_object(Config)
if os.environ.get('RENDER') and app.config['SECRET_KEY'].startswith('change-this'):
    raise RuntimeError('Set a long random SECRET_KEY environment variable before going live.')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
                  REMEMBER_COOKIE_DURATION=timedelta(days=14),
                  REMEMBER_COOKIE_HTTPONLY=True)
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
    checkout_request_id = db.Column(db.String(100))
    paid_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    chama = db.relationship('Chama', backref='subscription_payments')





class MerryGoRound(db.Model):
    __tablename__ = 'merry_go_rounds'
    id = db.Column(db.Integer, primary_key=True)
    chama_id = db.Column(db.Integer, db.ForeignKey('chamas.id'), nullable=False)
    name = db.Column(db.String(120), default='Merry Go Round')
    amount_per_member = db.Column(db.Float, nullable=False)
    frequency = db.Column(db.String(20), default='monthly')  # monthly, weekly
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chama = db.relationship('Chama', backref='merry_go_rounds')
    rounds = db.relationship('MGRRound', backref='mgr', lazy=True, order_by='MGRRound.round_number')


class MGRRound(db.Model):
    __tablename__ = 'mgr_rounds'
    id = db.Column(db.Integer, primary_key=True)
    mgr_id = db.Column(db.Integer, db.ForeignKey('merry_go_rounds.id'), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    scheduled_date = db.Column(db.Date)
    pot_amount = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='upcoming')  # upcoming, paid, skipped
    paid_date = db.Column(db.Date)

    recipient = db.relationship('User')


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

# ==================== SECURITY ====================

@app.before_request
def csrf_protect():
    if request.method == 'POST' and request.endpoint != 'mpesa_callback':
        token = session.get('_csrf')
        sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
        if not token or not sent or not hmac.compare_digest(token, sent):
            flash('Your session expired. Please try again.', 'danger')
            return redirect(request.referrer or url_for('index'))


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp


_attempts = {}


def too_many_attempts(key, limit=6, window=600):
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = hits
    return len(hits) >= limit


def record_attempt(key):
    _attempts.setdefault(key, []).append(time.time())


def safe_next(target):
    if not target:
        return None
    p = urlparse(target)
    return target if not p.netloc and not p.scheme and target.startswith('/') and not target.startswith('//') else None


def parse_amount(raw, minimum=1, maximum=10_000_000):
    try:
        v = round(float(str(raw).replace(',', '').strip()), 2)
    except (TypeError, ValueError):
        return None
    return v if minimum <= v <= maximum else None


ROLES = ('member', 'treasurer', 'secretary', 'chairperson')

# ==================== HELPERS ====================

def normalize_phone(phone):
    phone = (phone or '').strip().replace(' ', '').replace('-', '').replace('+', '')
    if phone.startswith('254') and len(phone) >= 12:
        phone = '0' + phone[3:]
    if len(phone) == 9 and phone[0] in '17':
        phone = '0' + phone
    return phone




def send_sms(phone, message, chama_id=None, purpose='general'):
    """Send SMS via Africa's Talking (or simulate)."""
    phone = normalize_phone(phone)
    try:
        from services.sms import SMSService
        result = SMSService().send(phone, message)
    except Exception as e:
        print('SMS error:', e)
        result = {'success': False, 'message': str(e)}
    if chama_id:
        db.session.add(SMSLog(chama_id=chama_id, phone=phone, message=message, purpose=purpose))
        db.session.commit()
    return result.get('success', False)


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
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
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
        key = f'{phone}|{request.remote_addr}'
        if too_many_attempts(key):
            flash('Too many attempts. Wait 10 minutes and try again.', 'danger')
            return render_template('login.html')
        user = User.query.filter_by(phone=phone).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(safe_next(request.args.get('next')) or url_for('dashboard'))
        record_attempt(key)
        flash('Wrong phone number or password.', 'danger')
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
        amount = parse_amount(request.form.get('contribution_amount') or 1000, 1) or 1000
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
            contribution_day=min(28, max(1, int(request.form.get('contribution_day') or 5))),
            loan_interest_rate=min(50.0, max(0.0, float(request.form.get('loan_interest_rate') or 5))),
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
    paid_ids = {c.user_id for c in Contribution.query.filter_by(chama_id=chama_id, month=date.today().strftime('%Y-%m')).all()}
    return render_template(
        'chama_home.html', paid_ids=paid_ids,
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

        if not app.config['MPESA_LIVE']:
            code = (request.form.get('mpesa_code') or '').strip().upper()
            if len(code) < 8:
                flash('Enter the M-Pesa confirmation code from your payment message.', 'danger')
                return redirect(url_for('subscription_pay', chama_id=chama.id))
            if SubscriptionPayment.query.filter_by(mpesa_receipt=code).first():
                flash('That M-Pesa code has already been submitted.', 'warning')
                return redirect(url_for('subscription_pay', chama_id=chama.id))
            db.session.add(SubscriptionPayment(chama_id=chama.id, amount=amount, months=months, phone=phone,
                                               mpesa_receipt=code, paid_by=current_user.id, status='pending'))
            chama.plan = chama.plan if chama.is_locked() else plan
            db.session.commit()
            flash('Payment received for checking. We unlock your chama as soon as it is confirmed, usually within a few hours.', 'success')
            return redirect(url_for('subscription_pay', chama_id=chama.id))

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

        # STK Push (real or simulated)
        try:
            from services.mpesa import MpesaService
            mpesa = MpesaService()
            result = mpesa.stk_push(
                phone=phone,
                amount=amount,
                account_ref=f'CHAMA{chama.id}',
                description=f'{PLANS[plan]["name"]} sub',
            )
        except Exception as e:
            result = {'success': False, 'message': str(e)}

        if not result.get('success'):
            payment.status = 'failed'
            db.session.commit()
            flash(result.get('message') or 'Payment failed. Try again.', 'danger')
            return redirect(url_for('subscription_pay', chama_id=chama.id))

        payment.checkout_request_id = result.get('checkout_request_id')
        db.session.commit()

        if result.get('simulated'):
            # Instant unlock in simulation mode
            payment.status = 'completed'
            payment.mpesa_receipt = f'SIM{random.randint(100000,999999)}'
            payment.completed_at = datetime.utcnow()
            chama.plan = plan
            chama.unlock_until(months)
            db.session.commit()
            send_sms(
                phone,
                f"ChamaPay: Payment KES {amount:,.0f} received for {chama.name}. Unlocked {months} month(s) on {PLANS[plan]['name']} plan. Asante!",
                chama_id=chama.id,
                purpose='payment_confirm',
            )
            flash(f'Payment of KES {amount:,.0f} received (test mode). Chama unlocked for {months} month(s)!', 'success')
            return redirect(url_for('chama_home', chama_id=chama.id))

        flash('M-Pesa prompt sent to your phone. Enter PIN. Chama unlocks automatically after payment.', 'info')
        return redirect(url_for('subscription_pay', chama_id=chama.id))

    return render_template(
        'subscribe.html',
        chama=chama,
        plans=PLANS,
        current_plan=chama.plan or 'basic',
        locked=chama.is_locked(),
        live=app.config['MPESA_LIVE'],
        pay_info=app.config['PAY_INSTRUCTIONS'],
        pending=SubscriptionPayment.query.filter_by(chama_id=chama.id, status='pending').count(),
    )


@app.route('/mpesa/callback/<secret>', methods=['POST'])
def mpesa_callback(secret):
    """Safaricom STK callback. The URL carries a secret so strangers cannot fake payments."""
    expected = app.config.get('MPESA_CALLBACK_SECRET', '')
    if not expected or not hmac.compare_digest(secret, expected):
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Forbidden'}), 403
    try:
        data = request.get_json(force=True) or {}
        body = data.get('Body', {}).get('stkCallback', {})
        checkout_id = body.get('CheckoutRequestID')
        payment = SubscriptionPayment.query.filter_by(
            checkout_request_id=checkout_id, status='pending').first() if checkout_id else None
        if payment and body.get('ResultCode') == 0:
            items = {i['Name']: i.get('Value') for i in body.get('CallbackMetadata', {}).get('Item', [])}
            paid = float(items.get('Amount') or 0)
            if paid + 0.5 >= payment.amount:
                payment.status = 'completed'
                payment.mpesa_receipt = str(items.get('MpesaReceiptNumber') or '')
                payment.completed_at = datetime.utcnow()
                chama = db.session.get(Chama, payment.chama_id)
                if chama:
                    chama.unlock_until(payment.months or 1)
                db.session.commit()
                if payment.phone:
                    send_sms(payment.phone, f"ChamaPay: Payment KES {payment.amount:,.0f} confirmed. "
                             f"Receipt {payment.mpesa_receipt}. Chama unlocked. Asante!",
                             chama_id=payment.chama_id, purpose='payment_confirm')
        elif payment:
            payment.status = 'failed'
            db.session.commit()
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})
    except Exception as e:
        print('Callback error:', e)
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})

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
    return render_template('members.html', chama=chama, members=members_list, is_admin=is_admin, locked=chama.is_locked(),
                           limit=PLANS.get(chama.plan, PLANS['basic'])['max_members'])


@app.route('/chama/<int:chama_id>/add_member', methods=['POST'])
@login_required
@require_unlocked
def add_member(chama_id):
    if not is_chama_admin(current_user, chama_id):
        flash('Only chairperson/treasurer can add members.', 'danger')
        return redirect(url_for('members', chama_id=chama_id))
    phone = normalize_phone(request.form.get('phone', ''))
    role = request.form.get('role', 'member')
    if role not in ROLES:
        role = 'member'
    chama = db.session.get(Chama, chama_id)
    limit = PLANS.get(chama.plan, PLANS['basic'])['max_members']
    if Membership.query.filter_by(chama_id=chama_id, is_active=True).count() >= limit:
        flash(f'Your {chama.plan_name()} plan allows up to {limit} members. Upgrade to add more.', 'warning')
        return redirect(url_for('members', chama_id=chama_id))
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
    try:
        user_id = int(request.form.get('user_id'))
    except (TypeError, ValueError):
        user_id = None
    amount = parse_amount(request.form.get('amount'))
    method = request.form.get('payment_method', 'Cash')
    if method not in ('Cash', 'M-Pesa', 'Bank'):
        method = 'Cash'
    mpesa = (request.form.get('mpesa_code', '') or '').strip().upper()[:30]
    if not user_id or not get_membership(user_id, chama_id):
        flash('Choose a member of this chama.', 'danger')
        return redirect(url_for('contributions', chama_id=chama_id))
    if amount is None:
        flash('Enter a valid amount greater than zero.', 'danger')
        return redirect(url_for('contributions', chama_id=chama_id))
    if mpesa and Contribution.query.filter_by(chama_id=chama_id, mpesa_code=mpesa).first():
        flash(f'M-Pesa code {mpesa} has already been recorded.', 'warning')
        return redirect(url_for('contributions', chama_id=chama_id))
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
    amount = parse_amount(request.form.get('amount'), 100)
    purpose = (request.form.get('purpose', '') or '').strip()[:200]
    if amount is None:
        flash('Enter a valid loan amount (minimum KES 100).', 'danger')
        return redirect(url_for('loans', chama_id=chama_id))
    if Loan.query.filter(Loan.chama_id == chama_id, Loan.user_id == current_user.id,
                         Loan.status.in_(['pending', 'active'])).first():
        flash('You already have a loan that is pending or unpaid.', 'warning')
        return redirect(url_for('loans', chama_id=chama_id))
    max_loan = membership.total_savings * chama.max_loan_multiplier
    if amount > max_loan:
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
    if loan.status != 'pending':
        flash('This loan has already been decided.', 'warning')
        return redirect(url_for('loans', chama_id=loan.chama_id))
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
    if loan.status != 'active':
        flash('Only active loans can be repaid.', 'warning')
        return redirect(url_for('loans', chama_id=loan.chama_id))
    amount = parse_amount(request.form.get('amount'))
    remaining = loan.total_repayable - loan.amount_paid
    amount = min(amount, remaining) if amount else 0
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


# ==================== MERRY GO ROUND ====================

@app.route('/chama/<int:chama_id>/mgr')
@login_required
def merry_go_round(chama_id):
    chama = db.session.get(Chama, chama_id)
    if not get_membership(current_user.id, chama_id) and not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    mgr = MerryGoRound.query.filter_by(chama_id=chama_id, is_active=True).first()
    members = Membership.query.filter_by(chama_id=chama_id, is_active=True).all()
    return render_template(
        'mgr.html', chama=chama, mgr=mgr, members=members,
        is_admin=is_chama_admin(current_user, chama_id),
        locked=chama.is_locked(),
    )


@app.route('/chama/<int:chama_id>/mgr/create', methods=['POST'])
@login_required
@require_unlocked
def create_mgr(chama_id):
    if not is_chama_admin(current_user, chama_id):
        flash('Only admin can create Merry Go Round.', 'danger')
        return redirect(url_for('merry_go_round', chama_id=chama_id))
    # deactivate old
    for old in MerryGoRound.query.filter_by(chama_id=chama_id, is_active=True).all():
        old.is_active = False
    name = request.form.get('name', 'Merry Go Round').strip() or 'Merry Go Round'
    amount = parse_amount(request.form.get('amount_per_member')) or 0
    frequency = request.form.get('frequency', 'monthly')
    if frequency not in ('monthly', 'weekly'):
        frequency = 'monthly'
    if amount <= 0:
        flash('Enter a valid amount per member.', 'danger')
        return redirect(url_for('merry_go_round', chama_id=chama_id))
    members = Membership.query.filter_by(chama_id=chama_id, is_active=True).all()
    if len(members) < 2:
        flash('Need at least 2 members.', 'danger')
        return redirect(url_for('merry_go_round', chama_id=chama_id))
    mgr = MerryGoRound(
        chama_id=chama_id, name=name,
        amount_per_member=amount, frequency=frequency,
    )
    db.session.add(mgr)
    db.session.flush()
    pot = amount * len(members)
    start = date.today()
    for i, m in enumerate(members):
        if frequency == 'weekly':
            sched = start + timedelta(weeks=i)
        else:
            sched = start + relativedelta(months=i)
        db.session.add(MGRRound(
            mgr_id=mgr.id,
            round_number=i + 1,
            recipient_id=m.user_id,
            scheduled_date=sched,
            pot_amount=pot,
            status='upcoming',
        ))
    db.session.commit()
    flash('Merry Go Round created with ' + str(len(members)) + ' rounds.', 'success')
    return redirect(url_for('merry_go_round', chama_id=chama_id))


@app.route('/mgr_round/<int:round_id>/mark_paid', methods=['POST'])
@login_required
def mark_mgr_paid(round_id):
    rnd = db.session.get(MGRRound, round_id)
    if not rnd:
        flash('Not found.', 'danger')
        return redirect(url_for('dashboard'))
    mgr = db.session.get(MerryGoRound, rnd.mgr_id)
    if not is_chama_admin(current_user, mgr.chama_id):
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    chama = db.session.get(Chama, mgr.chama_id)
    if chama.is_locked():
        flash('Chama locked. Renew subscription first.', 'warning')
        return redirect(url_for('subscription_pay', chama_id=chama.id))
    rnd.status = 'paid'
    rnd.paid_date = date.today()
    db.session.commit()
    if rnd.recipient:
        send_sms(
            rnd.recipient.phone,
            f"ChamaPay: Merry Go Round round {rnd.round_number} paid to you — KES {rnd.pot_amount:,.0f}. Asante!",
            chama_id=mgr.chama_id,
            purpose='mgr_payout',
        )
    flash(f'Round {rnd.round_number} marked paid to {rnd.recipient.name if rnd.recipient else "member"}.', 'success')
    return redirect(url_for('merry_go_round', chama_id=mgr.chama_id))


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
    pending = SubscriptionPayment.query.filter_by(status='pending').order_by(SubscriptionPayment.created_at).all()
    revenue = sum(p.amount for p in SubscriptionPayment.query.filter_by(status='completed').all())
    locked_count = sum(1 for c in chamas if c.subscription_status == 'locked')
    active_count = sum(1 for c in chamas if c.subscription_status in ('active', 'trial'))
    return render_template(
        'owner.html',
        chamas=chamas, payments=payments, revenue=revenue, pending=pending,
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
    months = max(1, min(int(request.form.get('months') or 1), 12))
    chama.unlock_until(months)
    flash(f'{chama.name} unlocked for {months} month(s).', 'success')
    return redirect(url_for('owner_dashboard'))


@app.route('/owner/payment/<int:payment_id>/<action>', methods=['POST'])
@login_required
def owner_payment(payment_id, action):
    if not current_user.is_platform_owner:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    p = db.session.get(SubscriptionPayment, payment_id)
    if not p or p.status != 'pending' or action not in ('confirm', 'reject'):
        flash('Payment not found or already handled.', 'warning')
        return redirect(url_for('owner_dashboard'))
    if action == 'confirm':
        p.status = 'completed'
        p.completed_at = datetime.utcnow()
        chama = db.session.get(Chama, p.chama_id)
        chama.unlock_until(p.months or 1)
        send_sms(p.phone, f"ChamaPay: Payment KES {p.amount:,.0f} confirmed. {chama.name} is unlocked. Asante!",
                 chama_id=chama.id, purpose='payment_confirm')
        flash(f'Confirmed. {chama.name} unlocked.', 'success')
    else:
        p.status = 'failed'
        flash('Payment rejected.', 'info')
    db.session.commit()
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
    owner_phone = normalize_phone(app.config.get('OWNER_PHONE', ''))
    owner_pw = os.environ.get('OWNER_PASSWORD', '')
    if owner_phone and owner_pw and len(owner_pw) >= 8 and not User.query.filter_by(phone=owner_phone).first():
        owner = User(name='Platform Owner', phone=owner_phone, is_platform_owner=True)
        owner.set_password(owner_pw)
        db.session.add(owner)
        db.session.commit()
        print('Owner account created for', owner_phone)


with app.app_context():
    try:
        init_db()
    except Exception as e:
        print('DB init:', e)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
