# Part of HiPay by Join Future. See LICENSE file for full copyright and licensing details.

import hashlib
import hmac
import json
import time

from odoo.addons.payment.tests.common import PaymentCommon


class HiPayCommon(PaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.webhook_secret = 'whsec_unit_test_only_0000000000'
        cls.hipay = cls._prepare_provider('hipay', update_values={
            'hipay_api_url': 'https://hipay.example.test',
            'hipay_secret_key_test': 'hp_sk_test_UnitTestOnlyKey000000000000000',
            'hipay_webhook_secret_test': cls.webhook_secret,
        })
        cls.provider = cls.hipay
        cls.currency_bhd = cls._prepare_currency('BHD')
        cls.currency = cls.currency_bhd
        cls.amount = 12.345  # 3-decimal currency: 12345 fils.
        cls.amount_minor = 12345
        cls.payment_method_card = cls.env.ref('payment.payment_method_card')
        cls.payment_method_benefit = cls.env.ref('payment.payment_method_benefit')
        cls.payment_method = cls.payment_method_card
        cls.payment_method_id = cls.payment_method_card.id
        cls.payment_method_code = 'card'
        cls.pi_id = 'pi_UnitTest000000000000001'

    def _payment_intent(self, status='succeeded', **values):
        """ A PaymentIntent as `GET /v1/payment_intents/:id` returns it. """
        return dict({
            'id': self.pi_id,
            'object': 'payment_intent',
            'amount': self.amount_minor,
            'amount_received': self.amount_minor if status == 'succeeded' else 0,
            'currency': 'bhd',
            'status': status,
            'client_secret': f'{self.pi_id}_secret_unit',
            'payment_method_types': ['card'],
            'metadata': {'odoo_reference': self.reference, 'source': 'odoo'},
            'livemode': False,
            'last_payment_error': None,
        }, **values)

    def _event(self, event_type, obj, livemode=False):
        return {
            'id': 'evt_UnitTest0001',
            'object': 'event',
            'type': event_type,
            'api_version': '2026-06-01',
            'data': {'object': obj},
            'livemode': livemode,
            'created': int(time.time()),
        }

    def _sign(self, body, secret=None, timestamp=None):
        """ Build a `HiPay-Signature` header the way HiPay's webhook worker does. """
        timestamp = int(time.time()) if timestamp is None else timestamp
        secret = secret or self.webhook_secret
        digest = hmac.new(
            secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256
        ).hexdigest()
        return f't={timestamp},v1={digest}'

    @staticmethod
    def _body(event):
        return json.dumps(event, separators=(',', ':')).encode()
