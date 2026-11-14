# Part of HiPay by Join Future. See LICENSE file for full copyright and licensing details.

import time
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug import urls

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.addons.payment_joinfuture_hipay.controllers.main import HiPayController
from odoo.addons.payment_joinfuture_hipay.tests.common import HiPayCommon

MAKE_REQUEST = (
    'odoo.addons.payment_joinfuture_hipay.models.payment_provider.PaymentProvider._hipay_make_request'
)


class _FakeProduct(SimpleNamespace):
    """ Stands in for a product.product when `product` is not installed. """
    _fields = {'is_published': True}

    def __init__(self, product_id, name, image=True):
        super().__init__(
            id=product_id, display_name=name, image_128=b'img' if image else False,
            is_published=True,
        )

    def __bool__(self):
        return True


class _FakeTaxes:
    """ Stands in for an account.tax recordset: empty, or one percent tax. """

    def __init__(self, amount=None):
        self.amount = amount
        self.amount_type = 'percent'

    def __len__(self):
        return 0 if self.amount is None else 1

    def __bool__(self):
        return self.amount is not None


LOGGERS = (
    'odoo.addons.payment_joinfuture_hipay.controllers.main',
    'odoo.addons.payment_joinfuture_hipay.models.payment_transaction',
    'odoo.addons.payment_joinfuture_hipay.models.payment_provider',
)


@tagged('post_install', '-at_install')
class TestHiPay(HiPayCommon, PaymentHttpCommon):

    # === AMOUNTS === #

    def test_minor_units_use_the_currency_decimals(self):
        Tx = self.env['payment.transaction']
        self.assertEqual(Tx._hipay_to_minor_units(12.345, self.currency_bhd), 12345)
        self.assertEqual(Tx._hipay_to_minor_units(0.3, self.currency_bhd), 300)
        self.assertEqual(Tx._hipay_to_minor_units(1.005, self._prepare_currency('KWD')), 1005)
        self.assertEqual(Tx._hipay_to_minor_units(7.125, self._prepare_currency('OMR')), 7125)
        self.assertEqual(Tx._hipay_to_minor_units(25.5, self.currency_usd), 2550)
        self.assertEqual(Tx._hipay_to_minor_units(19.99, self._prepare_currency('SAR')), 1999)
        self.assertEqual(Tx._hipay_to_major_units(12345, self.currency_bhd), 12.345)
        self.assertEqual(Tx._hipay_to_major_units(2550, self.currency_usd), 25.5)

    def test_supported_currencies(self):
        currencies = self.hipay.available_currency_ids.mapped('name')
        for code in ('BHD', 'KWD', 'OMR', 'SAR', 'AED', 'USD'):
            self.assertIn(code, currencies)
        self.assertNotIn('EUR', currencies)

    def test_refund_is_supported(self):
        self.assertEqual(self.hipay.support_refund, 'partial')

    def test_state_picks_the_key(self):
        self.assertTrue(self.hipay._hipay_get_secret_key().startswith('hp_sk_test_'))
        with self.assertRaises(ValidationError):  # Enabled without a live key.
            self.hipay.state = 'enabled'
        with self.assertRaises(ValidationError):  # A test key in the live field.
            self.hipay.write({
                'hipay_secret_key_live': 'hp_sk_test_wrongPrefix', 'state': 'enabled',
            })
        self.hipay.write({'hipay_secret_key_live': 'hp_sk_live_UnitTestOnly', 'state': 'enabled'})
        self.assertTrue(self.hipay._hipay_get_secret_key().startswith('hp_sk_live_'))

    # === PAYMENT INTENT CREATION === #

    def test_payment_intent_payload(self):
        tx = self._create_transaction('redirect')
        payload = tx._hipay_prepare_payment_intent_payload()

        self.assertEqual(payload['amount'], 12345)
        self.assertEqual(payload['currency'], 'BHD')
        self.assertEqual(payload['payment_method_types'], ['card'])
        self.assertEqual(payload['metadata']['odoo_reference'], tx.reference)
        return_url = urls.url_parse(payload['return_url'])
        self.assertEqual(return_url.path, HiPayController._return_url)
        self.assertEqual(return_url.decode_query().get('reference'), tx.reference)

    def test_benefit_payload(self):
        tx = self._create_transaction('redirect', payment_method_id=self.payment_method_benefit.id)
        payload = tx._hipay_prepare_payment_intent_payload()
        self.assertEqual(payload['payment_method_types'], ['benefit'])

    @mute_logger(*LOGGERS)
    def test_rendering_creates_the_payment_intent_and_redirect_form(self):
        tx = self._create_transaction('redirect')
        with patch(MAKE_REQUEST, return_value=self._payment_intent('requires_payment_method')) \
                as request_mock:
            processing_values = tx._get_processing_values()

        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], '/v1/payment_intents')
        self.assertEqual(kwargs['payload']['amount'], 12345)
        self.assertTrue(kwargs['idempotency_key'].endswith(tx.reference))
        self.assertEqual(tx.provider_reference, self.pi_id)

        form_info = self._extract_values_from_html_form(processing_values['redirect_form_html'])
        self.assertEqual(
            form_info['action'], f'https://hipay.example.test/hosted/checkout/{self.pi_id}'
        )
        self.assertEqual(form_info['method'], 'get')

    def test_idempotency_key_is_stable_per_transaction(self):
        tx = self._create_transaction('redirect')
        self.assertEqual(tx._hipay_idempotency_key(), tx._hipay_idempotency_key())
        other = self._create_transaction('redirect', reference='Other Transaction')
        self.assertNotEqual(tx._hipay_idempotency_key(), other._hipay_idempotency_key())
        # A failed attempt is rolled back and its reference comes back on the next attempt,
        # maybe for a changed cart: the new record must not replay the old request.
        reference, old_key = tx.reference, tx._hipay_idempotency_key()
        tx.unlink()
        again = self._create_transaction('redirect', reference=reference)
        self.assertNotEqual(again._hipay_idempotency_key(), old_key)

    # === LINE ITEMS AND VAT === #

    def _details(self, lines, amount_tax, amount_total=None):
        """ Run `_hipay_prepare_order_details` on a fake document with the given lines. """
        tx = self._create_transaction('redirect', amount=amount_total or self.amount)
        document = SimpleNamespace(
            _name='sale.order', amount_tax=amount_tax, amount_total=tx.amount,
            currency_id=self.currency_bhd,
        )
        with patch.object(type(tx), '_hipay_get_document', return_value=document), \
                patch.object(type(tx), '_hipay_document_lines', return_value=lines):
            details = tx._hipay_prepare_order_details()
        if 'vat_amount' in details or details.get('line_items'):
            total = sum(i['amount'] * i['quantity'] for i in details.get('line_items', []))
            details['_sum'] = total + details.get('vat_amount', 0)
            details['_expected'] = tx._hipay_to_minor_units(tx.amount, tx.currency_id)
        return details

    def test_line_items_three_decimals_with_vat(self):
        vat10 = _FakeTaxes(10.0)
        lines = [
            ('Arabic Coffee Set', 2, 22.450, _FakeProduct(7, 'Arabic Coffee Set'), vat10),
            ('Dates Box\nPremium', 3, 4.125, _FakeProduct(8, 'Dates Box', image=False), vat10),
        ]
        # 22.450 + 4.125 = 26.575 excl. tax, VAT 2.658 (rounded per order), total 29.233.
        details = self._details(lines, amount_tax=2.658, amount_total=29.233)

        self.assertEqual(details['line_items'][0]['amount'], 11225)  # Unit price in fils.
        self.assertEqual(details['line_items'][0]['quantity'], 2)
        self.assertTrue(details['line_items'][0]['image_url'].endswith(
            '/web/image/product.product/7/image_256'))
        self.assertEqual(details['line_items'][1]['name'], 'Dates Box')
        self.assertEqual(details['line_items'][1]['amount'], 1375)
        self.assertNotIn('image_url', details['line_items'][1])  # No image: no key.
        self.assertEqual(details['vat_amount'], 2658)
        self.assertEqual(details['vat_rate_bps'], 1000)
        self.assertEqual(details['_sum'], details['_expected'])  # Holds to the fil.

    def test_line_items_shipping_is_its_own_line(self):
        vat10 = _FakeTaxes(10.0)
        lines = [
            ('Arabic Coffee Set', 1, 12.345, _FakeProduct(7, 'Arabic Coffee Set'), vat10),
            ('Express Delivery', 1, 1.500, _FakeProduct(9, 'Express Delivery', image=False), vat10),
        ]
        details = self._details(lines, amount_tax=1.385, amount_total=15.230)
        self.assertEqual(
            [(i['name'], i['amount'], i['quantity']) for i in details['line_items']],
            [('Arabic Coffee Set', 12345, 1), ('Express Delivery', 1500, 1)],
        )
        self.assertEqual(details['_sum'], details['_expected'])

    def test_line_items_hide_the_internal_reference(self):
        lines = [('[HIPAY-DEMO-1] Arabic Coffee Set\nHand made', 1, 12.345,
                  _FakeProduct(7, 'Arabic Coffee Set'), _FakeTaxes())]
        details = self._details(lines, amount_tax=0.0, amount_total=12.345)
        self.assertEqual(details['line_items'][0]['name'], 'Arabic Coffee Set')

    def test_line_items_non_divisible_quantity(self):
        lines = [('Saffron', 3, 10.000, _FakeProduct(10, 'Saffron'), _FakeTaxes())]
        details = self._details(lines, amount_tax=0.0, amount_total=10.000)
        # 10.000 / 3 is not a whole number of fils: one line with the total.
        self.assertEqual(details['line_items'], [{
            'name': 'Saffron × 3', 'amount': 10000, 'quantity': 1,
            'image_url': details['line_items'][0]['image_url'],
        }])
        self.assertEqual(details['_sum'], details['_expected'])

    def test_line_items_fractional_quantity(self):
        lines = [('Cardamom (kg)', 1.5, 6.000, _FakeProduct(11, 'Cardamom'), _FakeTaxes())]
        details = self._details(lines, amount_tax=0.0, amount_total=6.000)
        self.assertEqual(details['line_items'][0]['name'], 'Cardamom (kg) × 1.5')
        self.assertEqual(details['line_items'][0]['quantity'], 1)
        self.assertEqual(details['line_items'][0]['amount'], 6000)

    def test_line_items_without_tax(self):
        lines = [('Gift Card', 2, 20.000, _FakeProduct(12, 'Gift Card'), _FakeTaxes())]
        details = self._details(lines, amount_tax=0.0, amount_total=20.000)
        self.assertNotIn('vat_amount', details)
        self.assertNotIn('vat_rate_bps', details)
        self.assertEqual(details['line_items'][0]['amount'], 10000)
        self.assertEqual(details['_sum'], details['_expected'])

    def test_line_items_mixed_rates_send_no_rate(self):
        lines = [
            ('Coffee', 1, 10.000, _FakeProduct(7, 'Coffee'), _FakeTaxes(10.0)),
            ('Book', 1, 5.000, _FakeProduct(13, 'Book'), _FakeTaxes(5.0)),
        ]
        details = self._details(lines, amount_tax=1.250, amount_total=16.250)
        self.assertEqual(details['vat_amount'], 1250)
        self.assertNotIn('vat_rate_bps', details)
        self.assertEqual(details['_sum'], details['_expected'])

    def test_line_items_never_negative(self):
        vat10 = _FakeTaxes(10.0)
        lines = [
            ('Coffee', 1, 10.000, _FakeProduct(7, 'Coffee'), vat10),
            ('Promotion -10%', 1, -1.000, _FakeProduct(14, 'Promotion', image=False), vat10),
        ]
        details = self._details(lines, amount_tax=0.900, amount_total=9.900)
        self.assertTrue(all(i['amount'] >= 0 for i in details['line_items']))
        self.assertEqual(len(details['line_items']), 1)
        self.assertEqual(details['vat_amount'], 900)  # Lines + VAT no longer add up: HiPay
        self.assertNotEqual(details['_sum'], details['_expected'])  # shows "includes VAT".

    def test_no_document_sends_no_lines(self):
        tx = self._create_transaction('redirect')
        payload = tx._hipay_prepare_payment_intent_payload()
        self.assertNotIn('line_items', payload)
        self.assertNotIn('vat_amount', payload)

    def test_sale_order_lines_end_to_end(self):
        """ With `sale` installed: a real order with Bahrain VAT 10%. """
        if 'sale.order' not in self.env:
            self.skipTest("sale is not installed")
        tax = self.env['account.tax'].create({
            'name': 'VAT 10% (HiPay test)', 'amount': 10.0, 'amount_type': 'percent',
            'type_tax_use': 'sale',
        })
        product = self.env['product.product'].create({
            'name': 'Arabic Coffee Set', 'list_price': 12.345, 'taxes_id': [(6, 0, tax.ids)],
        })
        pricelist = self.env['product.pricelist'].create({
            'name': 'BHD (HiPay test)', 'currency_id': self.currency_bhd.id,
        })
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'pricelist_id': pricelist.id,
            'order_line': [(0, 0, {
                'product_id': product.id, 'product_uom_qty': 3, 'price_unit': 12.345,
                'tax_id': [(6, 0, tax.ids)],
            })],
        })
        self.assertEqual(order.currency_id, self.currency_bhd)
        tx = self._create_transaction(
            'redirect', amount=order.amount_total, sale_order_ids=[(6, 0, order.ids)],
        )
        payload = tx._hipay_prepare_payment_intent_payload()
        self.assertEqual(payload['line_items'][0]['amount'], 12345)
        self.assertEqual(payload['line_items'][0]['quantity'], 3)
        self.assertEqual(payload['vat_rate_bps'], 1000)
        total = sum(i['amount'] * i['quantity'] for i in payload['line_items'])
        self.assertEqual(total + payload['vat_amount'], payload['amount'])

    # === RETURN ROUTE === #

    def _return(self, tx, **params):
        url = self._build_url(HiPayController._return_url)
        params = dict({'reference': tx.reference, 'payment_intent': self.pi_id}, **params)
        return self._make_http_get_request(url, params=params)

    @mute_logger(*LOGGERS)
    def test_return_confirms_from_the_api_and_is_idempotent(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded')) as request_mock:
            self._return(tx, status='succeeded')
            self.assertEqual(tx.state, 'done')
            self._return(tx, status='succeeded')  # Refresh / back button.
        self.assertEqual(tx.state, 'done')
        self.assertEqual(request_mock.call_args[0][0], f'/v1/payment_intents/{self.pi_id}')
        self.assertEqual(request_mock.call_args[1]['method'], 'GET')

    @mute_logger(*LOGGERS)
    def test_return_does_not_trust_the_query_status(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._payment_intent('requires_payment_method')):
            self._return(tx, status='succeeded')
        self.assertEqual(tx.state, 'draft')

    @mute_logger(*LOGGERS, 'odoo.http')
    def test_return_rejects_another_payment_intent(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded')) as request_mock:
            response = self._return(tx, payment_intent='pi_SomeoneElse', status='succeeded')
        self.assertEqual(response.status_code, 403)
        request_mock.assert_not_called()
        self.assertEqual(tx.state, 'draft')

    @mute_logger(*LOGGERS)
    def test_return_cancel(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._payment_intent('canceled')):
            self._return(tx, status='canceled')
        self.assertEqual(tx.state, 'cancel')

    @mute_logger(*LOGGERS)
    def test_amount_mismatch_is_an_error(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded', amount=12340)):
            self._return(tx, status='succeeded')
        self.assertEqual(tx.state, 'error')

    @mute_logger(*LOGGERS)
    def test_declined_attempt_is_not_final(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        declined = self._payment_intent(
            'requires_payment_method', last_payment_error={'code': 'card_declined'}
        )
        with patch(MAKE_REQUEST, return_value=declined):
            tx._handle_notification_data('hipay', {
                'reference': tx.reference, 'payment_intent': declined,
            })
        self.assertEqual(tx.state, 'draft')
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded')):
            self._return(tx, status='succeeded')
        self.assertEqual(tx.state, 'done')

    # === WEBHOOK === #

    def _post_webhook(self, event, signature=None, **sign_kwargs):
        body = self._body(event)
        headers = {'Content-Type': 'application/json'}
        if signature is not False:
            headers['HiPay-Signature'] = signature or self._sign(body, **sign_kwargs)
        return self.opener.post(
            self._build_url(HiPayController._webhook_url), data=body, headers=headers
        )

    @mute_logger(*LOGGERS)
    def test_webhook_with_valid_signature_confirms(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        event = self._event('payment_intent.succeeded', self._payment_intent('succeeded'))
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded')):
            response = self._post_webhook(event)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(tx.state, 'done')
            # At-least-once delivery: the same event again changes nothing.
            self.assertEqual(self._post_webhook(event).status_code, 200)
        self.assertEqual(tx.state, 'done')

    @mute_logger(*LOGGERS, 'odoo.http')
    def test_webhook_with_wrong_secret_is_rejected(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        event = self._event('payment_intent.succeeded', self._payment_intent('succeeded'))
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded')) as request_mock:
            response = self._post_webhook(event, secret='whsec_not_the_right_one')
        self.assertEqual(response.status_code, 403)
        request_mock.assert_not_called()
        self.assertEqual(tx.state, 'draft')

    @mute_logger(*LOGGERS, 'odoo.http')
    def test_webhook_with_tampered_body_is_rejected(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        event = self._event('payment_intent.succeeded', self._payment_intent('succeeded'))
        signature = self._sign(self._body(event))
        event['data']['object']['amount'] = 1
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded')):
            response = self._post_webhook(event, signature=signature)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(tx.state, 'draft')

    @mute_logger(*LOGGERS, 'odoo.http')
    def test_webhook_with_stale_or_future_timestamp_is_rejected(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        event = self._event('payment_intent.succeeded', self._payment_intent('succeeded'))
        with patch(MAKE_REQUEST, return_value=self._payment_intent('succeeded')):
            old = self._post_webhook(event, timestamp=int(time.time()) - 301)
            future = self._post_webhook(event, timestamp=int(time.time()) + 301)
        self.assertEqual(old.status_code, 403)
        self.assertEqual(future.status_code, 403)
        self.assertEqual(tx.state, 'draft')

    @mute_logger(*LOGGERS, 'odoo.http')
    def test_webhook_without_signature_is_rejected(self):
        event = self._event('payment_intent.succeeded', self._payment_intent('succeeded'))
        self.assertEqual(self._post_webhook(event, signature=False).status_code, 403)

    @mute_logger(*LOGGERS)
    def test_webhook_payment_failed_keeps_the_transaction_open(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        pi = self._payment_intent(
            'requires_payment_method', last_payment_error={'code': 'card_declined'}
        )
        with patch(MAKE_REQUEST, return_value=pi):
            response = self._post_webhook(self._event('payment_intent.payment_failed', pi))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tx.state, 'draft')

    @mute_logger(*LOGGERS)
    def test_webhook_canceled(self):
        tx = self._create_transaction('redirect', provider_reference=self.pi_id)
        pi = self._payment_intent('canceled')
        with patch(MAKE_REQUEST, return_value=pi):
            self._post_webhook(self._event('payment_intent.canceled', pi))
        self.assertEqual(tx.state, 'cancel')

    @mute_logger(*LOGGERS)
    def test_webhook_for_an_unknown_payment_is_acknowledged(self):
        pi = self._payment_intent('succeeded', id='pi_NotFromOdoo', metadata={})
        with patch(MAKE_REQUEST, return_value=pi) as request_mock:
            response = self._post_webhook(self._event('payment_intent.succeeded', pi))
        self.assertEqual(response.status_code, 200)
        request_mock.assert_not_called()

    @mute_logger(*LOGGERS)
    def test_webhook_ignores_unhandled_events(self):
        with patch(MAKE_REQUEST) as request_mock:
            response = self._post_webhook(self._event('payout.paid', {'id': 'po_1'}))
        self.assertEqual(response.status_code, 200)
        request_mock.assert_not_called()

    # === REFUNDS === #

    def _refund(self, refund_id='re_UnitTest0001', amount=5000, status='succeeded'):
        return {
            'id': refund_id,
            'object': 'refund',
            'charge': 'ch_UnitTest0001',
            'amount': amount,
            'currency': 'bhd',
            'status': status,
            'reason': 'requested_by_customer',
            'fee_refunded': 0,
            'livemode': False,
        }

    @mute_logger(*LOGGERS)
    def test_partial_refund(self):
        tx = self._create_transaction('redirect', state='done', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._refund()) as request_mock:
            refund_tx = tx._send_refund_request(amount_to_refund=5.0)

        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], '/v1/refunds')
        self.assertEqual(kwargs['payload']['payment_intent'], self.pi_id)
        self.assertEqual(kwargs['payload']['amount'], 5000)  # 5.000 BHD in fils.
        self.assertEqual(kwargs['idempotency_key'], refund_tx._hipay_idempotency_key())
        self.assertEqual(refund_tx.amount, -5.0)
        self.assertEqual(refund_tx.provider_reference, 're_UnitTest0001')
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(refund_tx.source_transaction_id, tx)

    @mute_logger(*LOGGERS)
    def test_failed_refund(self):
        tx = self._create_transaction('redirect', state='done', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._refund(status='failed')):
            refund_tx = tx._send_refund_request(amount_to_refund=5.0)
        self.assertEqual(refund_tx.state, 'error')

    @mute_logger(*LOGGERS)
    def test_charge_refunded_webhook_adds_outside_refunds_once(self):
        tx = self._create_transaction('redirect', state='done', provider_reference=self.pi_id)
        with patch(MAKE_REQUEST, return_value=self._refund()):
            tx._send_refund_request(amount_to_refund=5.0)  # Requested from Odoo: re_UnitTest0001.

        refunds = {
            'object': 'list',
            'data': [self._refund(), self._refund('re_FromPortal0002', amount=2345)],
            'has_more': False,
        }
        charge = {
            'id': 'ch_UnitTest0001', 'object': 'charge', 'payment_intent': self.pi_id,
            'amount': self.amount_minor, 'amount_refunded': 7345, 'currency': 'bhd',
            'status': 'partially_refunded',
        }
        event = self._event('charge.refunded', charge)
        with patch(MAKE_REQUEST, return_value=refunds):
            self.assertEqual(self._post_webhook(event).status_code, 200)
            self.assertEqual(self._post_webhook(event).status_code, 200)

        refund_txs = tx.child_transaction_ids.filtered(lambda t: t.operation == 'refund')
        self.assertEqual(len(refund_txs), 2)
        portal_refund = refund_txs.filtered(lambda t: t.provider_reference == 're_FromPortal0002')
        self.assertEqual(portal_refund.amount, -2.345)
        self.assertEqual(portal_refund.state, 'done')
