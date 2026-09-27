"""M-Pesa Daraja STK Push — sandbox and production."""
import base64
import requests
from datetime import datetime
from flask import current_app


class MpesaService:
    def __init__(self):
        self.consumer_key = current_app.config.get('MPESA_CONSUMER_KEY', '')
        self.consumer_secret = current_app.config.get('MPESA_CONSUMER_SECRET', '')
        self.shortcode = current_app.config.get('MPESA_SHORTCODE', '174379')
        self.passkey = current_app.config.get('MPESA_PASSKEY', '')
        self.callback_url = current_app.config.get('MPESA_CALLBACK_URL', '')
        self.env = current_app.config.get('MPESA_ENV', 'sandbox')
        self.simulate = current_app.config.get('SIMULATE_PAYMENTS', True)

        if self.env == 'production':
            self.base = 'https://api.safaricom.co.ke'
        else:
            self.base = 'https://sandbox.safaricom.co.ke'

    def _token(self):
        url = f'{self.base}/oauth/v1/generate?grant_type=client_credentials'
        r = requests.get(url, auth=(self.consumer_key, self.consumer_secret), timeout=30)
        r.raise_for_status()
        return r.json()['access_token']

    def _password(self, timestamp):
        data = f'{self.shortcode}{self.passkey}{timestamp}'
        return base64.b64encode(data.encode()).decode()

    def normalize_phone(self, phone):
        phone = (phone or '').strip().replace(' ', '').replace('+', '').replace('-', '')
        if phone.startswith('0'):
            phone = '254' + phone[1:]
        if phone.startswith('7') and len(phone) == 9:
            phone = '254' + phone
        return phone

    def stk_push(self, phone, amount, account_ref, description='ChamaPay subscription'):
        """
        Initiate STK Push.
        Returns dict: {success, simulated, checkout_request_id, merchant_request_id, message, raw}
        """
        phone = self.normalize_phone(phone)
        amount = int(round(float(amount)))
        if amount < 1:
            return {'success': False, 'message': 'Amount must be at least 1'}

        if self.simulate or not self.consumer_key:
            return {
                'success': True,
                'simulated': True,
                'checkout_request_id': f'sim_{datetime.utcnow().timestamp()}',
                'merchant_request_id': 'sim_merchant',
                'message': 'Simulated STK Push — set SIMULATE_PAYMENTS=false and add Daraja keys for real prompts',
            }

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        payload = {
            'BusinessShortCode': self.shortcode,
            'Password': self._password(timestamp),
            'Timestamp': timestamp,
            'TransactionType': 'CustomerPayBillOnline',
            'Amount': amount,
            'PartyA': phone,
            'PartyB': self.shortcode,
            'PhoneNumber': phone,
            'CallBackURL': self.callback_url,
            'AccountReference': (account_ref or 'ChamaPay')[:12],
            'TransactionDesc': (description or 'Payment')[:13],
        }
        try:
            token = self._token()
            url = f'{self.base}/mpesa/stkpush/v1/processrequest'
            r = requests.post(
                url,
                json=payload,
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                timeout=30,
            )
            data = r.json()
            if data.get('ResponseCode') == '0':
                return {
                    'success': True,
                    'simulated': False,
                    'checkout_request_id': data.get('CheckoutRequestID'),
                    'merchant_request_id': data.get('MerchantRequestID'),
                    'message': data.get('CustomerMessage', 'Check your phone for M-Pesa prompt'),
                    'raw': data,
                }
            return {
                'success': False,
                'message': data.get('errorMessage') or data.get('ResponseDescription') or str(data),
                'raw': data,
            }
        except Exception as e:
            return {'success': False, 'message': str(e)}
