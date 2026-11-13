# Part of HiPay by Join Future. See LICENSE file for full copyright and licensing details.

import hashlib
import hmac
import json
import logging
import time

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request
from odoo.tools import mute_logger

from odoo.addons.payment_hipay import const

_logger = logging.getLogger(__name__)


class HiPayController(http.Controller):
    _return_url = '/payment/hipay/return'
    _webhook_url = '/payment/hipay/webhook'

    @http.route(
        _return_url, type='http', auth='public', methods=['GET'], csrf=False, save_session=False
    )
    def hipay_return_from_checkout(self, **data):
        """ Handle the buyer coming back from the HiPay hosted checkout.

        HiPay sends the browser to the `return_url` of the PaymentIntent with `payment_intent` and
        `status` appended. Those query values are NOT trusted: the PaymentIntent is fetched again
        from the HiPay API with the secret key, and only that answer updates the transaction.
        Calling this route twice (refresh, back button) is harmless.

        The route is flagged with `save_session=False` so a redirect that arrives without the
        session cookie does not replace the buyer's session; `/payment/status` finds it again.

        :param dict data: `reference` (added by Odoo), `payment_intent` and `status` (added by
                          HiPay).
        """
        reference = data.get('reference')
        _logger.info(
            "Handling redirection from HiPay for reference %s (payment_intent=%s, status=%s)",
            reference, data.get('payment_intent'), data.get('status'),
        )
        if reference:
            try:
                tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
                    'hipay', {'reference': reference}
                )
                self._verify_and_process(tx_sudo, data.get('payment_intent'))
            except ValidationError:
                _logger.exception("Unable to handle the return from HiPay")
        with mute_logger('werkzeug'):
            return request.redirect('/payment/status')

    @staticmethod
    def _verify_and_process(tx_sudo, received_pi_id=None):
        """ Fetch the transaction's PaymentIntent from HiPay and apply it.

        :param recordset tx_sudo: The sudoed transaction, as a `payment.transaction` record.
        :param str received_pi_id: The PaymentIntent id the browser came back with, if any.
        :raise Forbidden: If the browser's PaymentIntent is not the transaction's.
        """
        pi_id = tx_sudo.provider_reference
        if not pi_id:
            raise ValidationError("HiPay: transaction %s has no PaymentIntent yet." % tx_sudo.reference)
        if received_pi_id and received_pi_id != pi_id:
            _logger.warning(
                "HiPay return for %s carried PaymentIntent %s instead of %s",
                tx_sudo.reference, received_pi_id, pi_id,
            )
            raise Forbidden()
        payment_intent = tx_sudo.provider_id._hipay_make_request(
            f'/v1/payment_intents/{pi_id}', method='GET'
        )
        metadata = payment_intent.get('metadata') or {}
        if metadata.get('odoo_reference') not in (None, tx_sudo.reference):
            _logger.warning("HiPay PaymentIntent %s carries another reference", pi_id)
            raise Forbidden()
        tx_sudo._handle_notification_data(
            'hipay', {'reference': tx_sudo.reference, 'payment_intent': payment_intent}
        )

    @http.route(_webhook_url, type='http', auth='public', methods=['POST'], csrf=False)
    def hipay_webhook(self):
        """ Handle a webhook event sent by HiPay.

        The `HiPay-Signature` header (`t=<unix>,v1=<hex>`) is checked over the raw request body
        before anything else: HMAC-SHA256 of `"{t}.{raw_body}"` with the endpoint's whsec_
        secret, compared in constant time, and refused when `t` is more than 5 minutes off.

        Delivery is at-least-once, so every branch is idempotent: payment events re-read the
        PaymentIntent and move the transaction only forward, and refund events skip refunds that
        already have a transaction.

        :return: A 2xx acknowledgement, or 403 for a bad signature.
        """
        raw_body = request.httprequest.get_data()
        try:
            event = json.loads(raw_body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            _logger.warning("HiPay webhook with an unreadable body")
            raise Forbidden()

        self._verify_signature(
            raw_body,
            request.httprequest.headers.get(const.SIGNATURE_HEADER),
            bool(event.get('livemode')),
        )

        event_type = event.get('type')
        _logger.info("HiPay webhook event %s (%s) received", event.get('id'), event_type)
        if event_type not in const.HANDLED_WEBHOOK_EVENTS:
            return request.make_json_response({'received': True})

        obj = (event.get('data') or {}).get('object') or {}
        try:
            if event_type.startswith('payment_intent.'):
                reference = (obj.get('metadata') or {}).get('odoo_reference')
                tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
                    'hipay', {'reference': reference, 'payment_intent_id': obj.get('id')}
                )
                self._verify_and_process(tx_sudo, obj.get('id'))
            elif event_type == 'charge.refunded':
                tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
                    'hipay', {'payment_intent_id': obj.get('payment_intent')}
                )
                tx_sudo._hipay_sync_refunds(obj)
        except ValidationError:
            # Not ours (another integration on the same HiPay account) or not processable:
            # acknowledge so HiPay does not retry forever.
            _logger.exception("Unable to handle the HiPay webhook event %s", event.get('id'))
        return request.make_json_response({'received': True})

    @staticmethod
    def _verify_signature(raw_body, header, livemode):
        """ Check the `HiPay-Signature` header against the webhook secrets of the providers.

        :param bytes raw_body: The exact request body.
        :param str header: The `HiPay-Signature` header value.
        :param bool livemode: The event's mode, which selects the test or live secret.
        :raise Forbidden: If the header is missing, stale, or matches no secret.
        """
        if not header:
            _logger.warning("HiPay webhook without signature")
            raise Forbidden()
        parts = {}
        for item in header.split(','):
            key, sep, value = item.strip().partition('=')
            if sep:
                parts.setdefault(key, []).append(value)
        try:
            timestamp = int(parts.get('t', [''])[0])
        except ValueError:
            _logger.warning("HiPay webhook with a malformed timestamp")
            raise Forbidden()
        signatures = parts.get('v1') or []
        if not signatures:
            _logger.warning("HiPay webhook without v1 signature")
            raise Forbidden()
        if abs(time.time() - timestamp) > const.WEBHOOK_TOLERANCE:
            _logger.warning("HiPay webhook with an outdated timestamp: %s", timestamp)
            raise Forbidden()

        providers = request.env['payment.provider'].sudo().search([('code', '=', 'hipay')])
        secrets = {p._hipay_get_webhook_secret(livemode) for p in providers} - {False, None, ''}
        if not secrets:
            _logger.warning("HiPay webhook ignored: no webhook secret is configured")
            raise Forbidden()

        signed_payload = str(timestamp).encode() + b'.' + raw_body
        for secret in secrets:
            expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
            if any(hmac.compare_digest(expected, received) for received in signatures):
                return
        _logger.warning("HiPay webhook with an invalid signature")
        raise Forbidden()
