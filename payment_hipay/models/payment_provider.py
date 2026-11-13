# Part of HiPay by Join Future. See LICENSE file for full copyright and licensing details.

import logging

import requests

from odoo import _, api, fields, models, service
from odoo.exceptions import ValidationError

from odoo.addons.payment_hipay import const

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('hipay', "HiPay")], ondelete={'hipay': 'set default'}
    )
    hipay_api_url = fields.Char(
        string="HiPay API URL",
        help="The base URL of the HiPay API. Keep the default unless Join Future gives you another"
             " one.",
        default=const.DEFAULT_API_URL,
        required_if_provider='hipay',
    )
    hipay_secret_key_test = fields.Char(
        string="Test Secret Key",
        help="The secret key that starts with hp_sk_test_. Used while the provider is in Test"
             " Mode.",
        groups='base.group_system',
    )
    hipay_secret_key_live = fields.Char(
        string="Live Secret Key",
        help="The secret key that starts with hp_sk_live_. Used while the provider is Enabled.",
        groups='base.group_system',
    )
    hipay_webhook_secret_test = fields.Char(
        string="Test Webhook Signing Secret",
        help="The whsec_ secret HiPay shows once when you add a test webhook endpoint.",
        groups='base.group_system',
    )
    hipay_webhook_secret_live = fields.Char(
        string="Live Webhook Signing Secret",
        help="The whsec_ secret HiPay shows once when you add a live webhook endpoint.",
        groups='base.group_system',
    )

    # === CONSTRAINT METHODS === #

    @api.constrains('state', 'code', 'hipay_secret_key_test', 'hipay_secret_key_live')
    def _check_hipay_secret_key_matches_state(self):
        """ The provider state picks the key: Test Mode needs a test key, Enabled a live key. """
        for provider in self.filtered(lambda p: p.code == 'hipay'):
            if provider.state == 'test':
                key, prefix = provider.hipay_secret_key_test, 'hp_sk_test_'
            elif provider.state == 'enabled':
                key, prefix = provider.hipay_secret_key_live, 'hp_sk_live_'
            else:
                continue
            if not key:
                raise ValidationError(_(
                    "HiPay: set the %(mode)s secret key before switching the provider to this"
                    " state.",
                    mode=_("test") if provider.state == 'test' else _("live"),
                ))
            if not key.startswith(prefix):
                raise ValidationError(_(
                    "HiPay: the key for this state must start with %(prefix)s.", prefix=prefix,
                ))

    # === COMPUTE METHODS === #

    def _compute_feature_support_fields(self):
        """ Override of `payment` to enable additional features. """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'hipay').update({
            'support_refund': 'partial',
        })

    # === BUSINESS METHODS === #

    def _get_supported_currencies(self):
        """ Override of `payment` to return the currencies HiPay accepts. """
        supported_currencies = super()._get_supported_currencies()
        if self.code == 'hipay':
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        default_codes = super()._get_default_payment_method_codes()
        if self.code != 'hipay':
            return default_codes
        return const.DEFAULT_PAYMENT_METHOD_CODES

    def _hipay_get_secret_key(self):
        """ Return the secret key of the provider's current mode.

        Note: self.ensure_one()

        :return: The test key in Test Mode, the live key otherwise.
        :rtype: str
        """
        self.ensure_one()
        sudo_self = self.sudo()
        if self.state == 'enabled':
            return sudo_self.hipay_secret_key_live
        return sudo_self.hipay_secret_key_test

    def _hipay_get_webhook_secret(self, livemode):
        """ Return the webhook signing secret for the mode of a received event.

        :param bool livemode: Whether the event was emitted in live mode.
        :return: The matching whsec_ secret, or False if none is set.
        :rtype: str
        """
        self.ensure_one()
        sudo_self = self.sudo()
        return sudo_self.hipay_webhook_secret_live if livemode else sudo_self.hipay_webhook_secret_test

    def _hipay_get_api_url(self):
        self.ensure_one()
        return (self.hipay_api_url or const.DEFAULT_API_URL).rstrip('/')

    def _hipay_make_request(
        self, endpoint, payload=None, method='POST', idempotency_key=None, params=None
    ):
        """ Make a request to the HiPay API and return its JSON response.

        HiPay speaks snake_case JSON with amounts in integer minor units, and authenticates with
        the secret key as a Bearer token. Errors come back in a bilingual envelope
        `{"error": {"code", "message_en", "message_ar", ...}}`.

        Note: self.ensure_one()

        :param str endpoint: The endpoint path, e.g. `/v1/payment_intents`.
        :param dict payload: The JSON body of the request.
        :param str method: The HTTP method.
        :param str idempotency_key: Sent as the `Idempotency-Key` header so that a retried
                                    request replays the first result instead of repeating it.
        :param dict params: The query string parameters.
        :return: The JSON-decoded response.
        :rtype: dict
        :raise ValidationError: If the API cannot be reached or answers with an error.
        """
        self.ensure_one()
        url = f'{self._hipay_get_api_url()}/{endpoint.lstrip("/")}'
        odoo_version = service.common.exp_version()['server_version']
        module = self.env.ref('base.module_payment_hipay', raise_if_not_found=False)
        module_version = (module and module.sudo().installed_version) or 'dev'
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {self._hipay_get_secret_key()}',
            'User-Agent': f'Odoo/{odoo_version} HiPayOdoo/{module_version}',
        }
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key

        try:
            response = requests.request(
                method, url, json=payload, params=params, headers=headers, timeout=60
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            _logger.exception("Unable to reach HiPay at %s", url)
            raise ValidationError("HiPay: " + _("Could not establish the connection to the API."))

        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.ok:
            error = data.get('error') or {}
            _logger.error(
                "HiPay API error on %s %s (HTTP %s): code=%s request_id=%s",
                method, endpoint, response.status_code, error.get('code'), error.get('request_id'),
            )
            lang = self.env.context.get('lang') or ''
            message = (error.get('message_ar') if lang.startswith('ar') else None) \
                or error.get('message_en') or response.reason
            raise ValidationError("HiPay: " + _(
                "The communication with the API failed. HiPay gave us the following information:"
                " %s", message,
            ))
        return data
