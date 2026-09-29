# ChamaPay Kenya — Subscription Chama Platform

Real platform for Kenyan Chamas. You charge **monthly subscription per Chama**. If they don't pay, the Chama is **locked** until payment — then **unlocked immediately**.

## Business model

| Item | Default |
|------|---------|
| Price | KES 500 / Chama / month |
| Trial | 14 days free |
| Lock | Automatic when trial/subscription ends |
| Unlock | Immediate after payment (or owner manual unlock) |

Change price with env var `MONTHLY_PRICE`.

## Features for Chamas

- Member registration (phone + password)
- Create Chama (starts trial)
- Contributions (Cash / M-Pesa code / Bank)
- Loans (apply, approve, repay)
- Members & roles (chairperson, treasurer, secretary, member)
- Locked state blocks new contributions/loans/members until paid

## Features for you (Owner)

- Owner dashboard: revenue, active vs locked Chamas
- Manual lock / unlock
- See all subscription payments

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

### Owner account

Set env `OWNER_PHONE` to your phone number, then register with that phone — you become platform owner.

Set `OWNER_PHONE` and `OWNER_PASSWORD` (8+ characters) and the owner account is created on first start. There is no default password.

## Deploy on Render

1. Push this folder to GitHub
2. New Web Service → connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `gunicorn app:app`
5. Env vars:

```
SECRET_KEY=long-random-string
OWNER_PHONE=07XXXXXXXX
MONTHLY_PRICE=500
TRIAL_DAYS=14
OWNER_PASSWORD=a-strong-password
PAY_INSTRUCTIONS=Send the fee to Till 123456, then enter the M-Pesa code.
```

The app refuses to start on Render without a real `SECRET_KEY`.

6. Optional: add Render PostgreSQL and set `DATABASE_URL`

## Taking subscription payments (manual, works today)

Chamas send the fee to your till or number, then enter the M-Pesa code on the Subscription page. You see it under "Payments to check" on the Owner page and tap "Confirm and unlock". Duplicate codes are rejected.

## M-Pesa (automatic, later)

Set:

```
SIMULATE_PAYMENTS=false
MPESA_CONSUMER_KEY=...
MPESA_CONSUMER_SECRET=...
MPESA_SHORTCODE=...
MPESA_PASSKEY=...
MPESA_CALLBACK_SECRET=long-random-string
MPESA_CALLBACK_URL=https://your-app.onrender.com/mpesa/callback/long-random-string
MPESA_ENV=sandbox
```

`SIMULATE_PAYMENTS=true` unlocks for free and is only for testing on your own computer.

## Stack

Python · Flask · SQLAlchemy · Bootstrap 5 · Gunicorn · Postgres/SQLite
