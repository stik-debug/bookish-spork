"""Africa's Talking SMS — sandbox and production."""
import requests
from flask import current_app


class SMSService:
    def __init__(self):
        self.username = current_app.config.get('AT_USERNAME', 'sandbox')
        self.api_key = current_app.config.get('AT_API_KEY', '')
        self.sender_id = current_app.config.get('AT_SENDER_ID', 'CHAMAAPP')
        self.simulate = current_app.config.get('SIMULATE_SMS', True)

    def normalize_phone(self, phone):
        phone = (phone or '').strip().replace(' ', '').replace('-', '').replace('+', '')
        if phone.startswith('0'):
            phone = '254' + phone[1:]
        if len(phone) == 9 and phone[0] in '17':
            phone = '254' + phone
        return phone

    def send(self, phone, message):
        """
        Send SMS.
        Returns {success, simulated, message, raw}
        """
        phone = self.normalize_phone(phone)
        if self.simulate or not self.api_key:
            print(f'[SMS SIM] to +{phone}: {message}')
            return {
                'success': True,
                'simulated': True,
                'message': 'Simulated SMS (set SIMULATE_SMS=false and AT_API_KEY for real SMS)',
            }

        url = 'https://api.africastalking.com/version1/messaging'
        headers = {
            'apiKey': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
        }
        data = {
            'username': self.username,
            'to': f'+{phone}',
            'message': message,
        }
        # Sender ID only in production with approved name
        if self.username != 'sandbox' and self.sender_id:
            data['from'] = self.sender_id

        try:
            r = requests.post(url, headers=headers, data=data, timeout=30)
            raw = r.json() if r.content else {}
            recipients = (raw.get('SMSMessageData') or {}).get('Recipients') or []
            ok = bool(recipients) and recipients[0].get('statusCode') in (100, 101, 102)
            return {
                'success': ok or r.status_code == 201,
                'simulated': False,
                'message': recipients[0].get('status') if recipients else r.text[:200],
                'raw': raw,
            }
        except Exception as e:
            return {'success': False, 'simulated': False, 'message': str(e)}
