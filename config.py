import os
from dotenv import load_dotenv

load_dotenv()

# Subscription plans (KES per month)
PLANS = {
    'basic': {
        'name': 'Basic',
        'price': float(os.environ.get('PLAN_BASIC', '500')),
        'max_members': 15,
        'features': ['Contributions', 'Loans', 'Members', 'Basic reports'],
    },
    'standard': {
        'name': 'Standard',
        'price': float(os.environ.get('PLAN_STANDARD', '1000')),
        'max_members': 40,
        'features': ['Everything in Basic', 'More members', 'Priority support'],
    },
    'premium': {
        'name': 'Premium',
        'price': float(os.environ.get('PLAN_PREMIUM', '2000')),
        'max_members': 100,
        'features': ['Everything in Standard', 'Up to 100 members', 'SMS reminders'],
    },
}

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-production-2026')

    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql+psycopg2://', 1)
        elif database_url.startswith('postgresql://') and '+psycopg' not in database_url:
            database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)

    SQLALCHEMY_DATABASE_URI = database_url or 'sqlite:///chama_platform.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    MONTHLY_PRICE = float(os.environ.get('MONTHLY_PRICE', '500'))  # default = basic
    TRIAL_DAYS = int(os.environ.get('TRIAL_DAYS', '14'))
    REMINDER_DAYS_BEFORE = int(os.environ.get('REMINDER_DAYS_BEFORE', '3'))

    OWNER_PHONE = os.environ.get('OWNER_PHONE', '0700000000')

    MPESA_CONSUMER_KEY = os.environ.get('MPESA_CONSUMER_KEY', '')
    MPESA_CONSUMER_SECRET = os.environ.get('MPESA_CONSUMER_SECRET', '')
    MPESA_SHORTCODE = os.environ.get('MPESA_SHORTCODE', '174379')
    MPESA_PASSKEY = os.environ.get('MPESA_PASSKEY', '')
    MPESA_CALLBACK_URL = os.environ.get('MPESA_CALLBACK_URL', '')
    MPESA_ENV = os.environ.get('MPESA_ENV', 'sandbox')
    SIMULATE_PAYMENTS = os.environ.get('SIMULATE_PAYMENTS', 'true').lower() == 'true'
    SIMULATE_SMS = os.environ.get('SIMULATE_SMS', 'true').lower() == 'true'

    # Africa's Talking
    AT_USERNAME = os.environ.get('AT_USERNAME', 'sandbox')
    AT_API_KEY = os.environ.get('AT_API_KEY', '')
    AT_SENDER_ID = os.environ.get('AT_SENDER_ID', 'CHAMAAPP')
