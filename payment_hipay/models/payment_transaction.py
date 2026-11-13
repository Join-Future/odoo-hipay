# Part of HiPay by Join Future. See LICENSE file for full copyright and licensing details.

import logging
import pprint
import re
from decimal import Decimal

from werkzeug import urls

from odoo import _, api, models
from odoo.exceptions import ValidationError
from odoo.tools import float_repr

from odoo.addons.payment_hipay import const
from odoo.addons.payment_hipay.controllers.main import HiPayController

_logger = logging.getLogger(__name__)


def _redact(data):
    """ Return a copy of an API object that is safe to log. """
    if not isinstance(data, dict):
        return data
    return {k: ('***' if k in const.SENSITIVE_KEYS else v) for k, v in data.items()}


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # === AMOUNT CONVERSION === #

    @api.model
    def _hipay_decimals(self, currency):
        """ Return the minor-unit decimal places HiPay uses for a currency.

        HiPay's own exponent table is authoritative for the wire format; it matches ISO 4217 and
        Odoo's `decimal_places` for every supported currency (3 for BHD/KWD/OMR/JOD).
        """
        return const.CURRENCY_DECIMALS.get(currency.name, currency.decimal_places)

    @api.model
    def _hipay_to_minor_units(self, amount, currency):
        """ Convert a major-unit amount to HiPay's integer minor units, without float drift.

        12.345 BHD -> 12345, 25.5 USD -> 2550.
        """
        decimals = self._hipay_decimals(currency)
        return int(Decimal(float_repr(amount, decimals)).scaleb(decimals))

    @api.model
    def _hipay_to_major_units(self, minor_amount, currency):
        """ Convert HiPay's integer minor units back to a major-unit amount. """
        decimals = self._hipay_decimals(currency)
        return float(Decimal(int(minor_amount)).scaleb(-decimals))

    # === PAYMENT FLOW === #

    def _get_specific_rendering_values(self, processing_values):
        """ Override of `payment` to create the HiPay PaymentIntent and return the redirect form.

        Note: self.ensure_one() from `_get_processing_values`

        :param dict processing_values: The generic and specific processing values of the
                                       transaction.
        :return: The dict of provider-specific rendering values.
        :rtype: dict
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'hipay':
            return res

        payload = self._hipay_prepare_payment_intent_payload()
        payment_intent = self.provider_id._hipay_make_request(
            '/v1/payment_intents', payload=payload, idempotency_key=self._hipay_idempotency_key(),
        )
        _logger.info(
            "HiPay PaymentIntent %s created for transaction %s:\n%s",
            payment_intent.get('id'), self.reference, pprint.pformat(_redact(payment_intent)),
        )
        # Store the PaymentIntent id now, so the return and webhook routes can check that what
        # they receive belongs to this transaction.
        self.provider_reference = payment_intent['id']

        api_url = f'{self.provider_id._hipay_get_api_url()}/hosted/checkout/{payment_intent["id"]}'
        url_params = {}
        lang = (self.partner_lang or self.env.context.get('lang') or '')[:2]
        if lang in ('ar', 'en'):
            url_params['locale'] = lang
        return {'api_url': api_url, 'url_params': url_params}

    def _hipay_idempotency_key(self):
        """ A stable key per transaction: a retry replays the first PaymentIntent.

        The record id is part of the key because a reference can come back: when an attempt
        fails, Odoo rolls the transaction back and the next attempt (maybe for a changed cart)
        gets the same reference again. Ids come from a sequence and are never reused.
        """
        db_uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid', '')
        return f'odoo-{db_uuid[:8]}-{self.id}-{self.reference}'

    def _hipay_prepare_payment_intent_payload(self):
        """ Build the `POST /v1/payment_intents` body for this transaction. """
        base_url = self.provider_id.get_base_url()
        return_url = urls.url_join(base_url, HiPayController._return_url)
        method_type = const.PAYMENT_METHODS_MAPPING.get(
            self.payment_method_code, const.DEFAULT_HIPAY_METHOD
        )
        payload = {
            'amount': self._hipay_to_minor_units(self.amount, self.currency_id),
            'currency': self.currency_id.name,
            'description': self.reference,
            # HiPay appends `payment_intent` and `status` to this URL when it sends the buyer
            # back. The reference lets the return route find the transaction.
            'return_url': f'{return_url}?{urls.url_encode({"reference": self.reference})}',
            'payment_method_types': [method_type],
            'metadata': {
                'odoo_reference': self.reference,
                'source': 'odoo',
            },
            'checkout_details': {'reference': self.reference[:40]},
        }
        payload.update(self._hipay_prepare_order_details())
        partner = self.partner_id
        if self.partner_name or partner.name:
            payload['customer_name'] = (self.partner_name or partner.name)[:200]
        email = self.partner_email or partner.email
        if email and '@' in email:
            payload['customer_email'] = email.strip()[:320]
        phone = self.partner_phone or partner.phone
        if phone:
            payload['customer_phone'] = phone[:40]
        return payload

    # === ORDER DETAILS SHOWN ON THE CHECKOUT === #

    def _hipay_get_document(self):
        """ Return the sale order or invoice this transaction pays, or None.

        Only a single document paid in full, in the transaction's currency, is used: a partial
        payment (deposit, installment) shows no item list because its lines would not add up to
        the amount charged.
        """
        documents = []
        if 'sale_order_ids' in self._fields and len(self.sale_order_ids) == 1:
            documents.append(self.sale_order_ids)
        elif 'invoice_ids' in self._fields and len(self.invoice_ids) == 1:
            documents.append(self.invoice_ids)
        for document in documents:
            if document.currency_id == self.currency_id and self._hipay_to_minor_units(
                document.amount_total, self.currency_id
            ) == self._hipay_to_minor_units(self.amount, self.currency_id):
                return document
        return None

    def _hipay_document_lines(self, document):
        """ Return `(name, quantity, subtotal excl. tax, product, taxes)` for each priced line. """
        lines = []
        if document._name == 'sale.order':
            order_lines = document.order_line.filtered(lambda l: not l.display_type)
            # Delivery last, as its own line.
            if 'is_delivery' in order_lines._fields:
                order_lines = order_lines.sorted(lambda l: l.is_delivery)
            for line in order_lines:
                if getattr(line, 'is_delivery', False) and line.currency_id.is_zero(line.price_subtotal):
                    continue  # Free delivery adds nothing to what the buyer pays.
                lines.append((
                    line.name, line.product_uom_qty, line.price_subtotal, line.product_id,
                    line.tax_id,
                ))
        else:  # account.move
            for line in document.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
                lines.append((
                    line.name or line.product_id.display_name, line.quantity, line.price_subtotal,
                    line.product_id, line.tax_ids,
                ))
        return lines

    def _hipay_image_url(self, product):
        """ The absolute URL of a product image the buyer's browser can load, or None. """
        if not product or not product.image_128:
            return None
        # A public visitor can only load the image of a product published on the website.
        if 'is_published' not in product._fields or not product.is_published:
            return None
        base_url = self.provider_id.get_base_url()
        return urls.url_join(base_url, f'/web/image/product.product/{product.id}/image_256')

    def _hipay_vat_rate_bps(self, taxes_per_line):
        """ The VAT rate in basis points when every taxed line has the same single percent tax. """
        rates = set()
        for taxes in taxes_per_line:
            if not taxes:
                continue
            if len(taxes) != 1 or taxes.amount_type != 'percent':
                return None
            rates.add(taxes.amount)
        if len(rates) != 1:
            return None
        bps = Decimal(str(rates.pop())) * 100
        return int(bps) if bps == bps.to_integral_value() and 0 <= bps <= 10000 else None

    def _hipay_prepare_order_details(self):
        """ Build `line_items`, `vat_amount` and `vat_rate_bps` for the hosted checkout.

        Each line carries the UNIT price excluding tax in minor units, after discounts. HiPay
        draws `amount x quantity` per line, then a VAT row, then the total, when
        `sum(lines) + vat_amount == amount`. Lines whose total is not a whole number of minor
        units per unit are sent as one line with "x qty" in the name, so the sum holds to the
        fil. A negative line (a promotion or a deposit already paid) is left out; the sum then no
        longer matches and HiPay shows the lines with an "includes VAT" note instead.

        :return: The keys to merge into the PaymentIntent payload (possibly none).
        :rtype: dict
        """
        document = self._hipay_get_document()
        if not document:
            return {}

        currency = self.currency_id
        line_items, taxes_per_line = [], []
        for name, quantity, subtotal, product, taxes in self._hipay_document_lines(document):
            subtotal_minor = self._hipay_to_minor_units(subtotal, currency)
            if subtotal_minor < 0:
                continue  # Never send a negative amount.
            if not quantity and not subtotal_minor:
                continue
            name = ((name or product.display_name or '').strip().splitlines() or [''])[0]
            # Odoo puts the internal reference first ("[FURN_001] Desk"); the buyer sees the name.
            name = re.sub(r'^\[[^\]]*\]\s*', '', name).strip() or (product.name or _("Item"))
            qty = Decimal(str(quantity or 0))
            if qty > 0 and qty == qty.to_integral_value() and subtotal_minor % int(qty) == 0:
                item = {'name': name[:255], 'amount': subtotal_minor // int(qty),
                        'quantity': int(qty)}
            else:
                qty_label = format(qty.normalize(), 'f') if qty else '0'
                item = {'name': f'{name[:240]} × {qty_label}', 'amount': subtotal_minor,
                        'quantity': 1}
            image_url = self._hipay_image_url(product)
            if image_url:
                item['image_url'] = image_url
            line_items.append(item)
            taxes_per_line.append(taxes)

        if not line_items:
            return {}
        details = {'line_items': line_items[:100]}
        vat_minor = self._hipay_to_minor_units(document.amount_tax, currency)
        if vat_minor > 0:
            details['vat_amount'] = vat_minor
            rate = self._hipay_vat_rate_bps(taxes_per_line)
            if rate:
                details['vat_rate_bps'] = rate
        return details

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        """ Override of `payment` to find the transaction based on HiPay data.

        :param str provider_code: The code of the provider that handled the transaction.
        :param dict notification_data: The normalized notification data: `reference` and/or
                                       `payment_intent_id`, plus the API object.
        :return: The transaction if found.
        :rtype: recordset of `payment.transaction`
        :raise ValidationError: If the data match no transaction.
        """
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'hipay' or len(tx) == 1:
            return tx

        reference = notification_data.get('reference')
        pi_id = notification_data.get('payment_intent_id')
        refund = notification_data.get('refund')
        domain = [('provider_code', '=', 'hipay')]
        if refund:
            domain += [('provider_reference', '=', refund.get('id')), ('operation', '=', 'refund')]
        elif reference:
            domain += [('reference', '=', reference)]
        elif pi_id:
            domain += [('provider_reference', '=', pi_id), ('operation', '!=', 'refund')]
        else:
            raise ValidationError("HiPay: " + _("Received data with missing reference."))

        tx = self.search(domain, limit=1)
        if not tx:
            raise ValidationError("HiPay: " + _(
                "No transaction found matching reference %s.", reference or pi_id
            ))
        return tx

    def _process_notification_data(self, notification_data):
        """ Override of `payment` to update the transaction from HiPay data.

        `notification_data` always carries an object fetched from (or signed by) HiPay, never
        values taken from the buyer's browser.

        Note: self.ensure_one()

        :param dict notification_data: The normalized notification data.
        :return: None
        """
        super()._process_notification_data(notification_data)
        if self.provider_code != 'hipay':
            return

        if self.operation == 'refund':
            self._hipay_process_refund(notification_data.get('refund') or {})
            return

        payment_intent = notification_data.get('payment_intent') or {}
        if not payment_intent:
            raise ValidationError("HiPay: " + _("Received data with missing PaymentIntent."))

        # The PaymentIntent must be the one created for this transaction.
        if self.provider_reference and payment_intent.get('id') != self.provider_reference:
            raise ValidationError("HiPay: " + _(
                "The PaymentIntent %(received)s does not belong to transaction %(ref)s.",
                received=payment_intent.get('id'), ref=self.reference,
            ))
        self.provider_reference = payment_intent.get('id')

        # It must charge exactly what Odoo asked for.
        expected_amount = self._hipay_to_minor_units(self.amount, self.currency_id)
        received_currency = (payment_intent.get('currency') or '').upper()
        if (
            int(payment_intent.get('amount') or 0) != expected_amount
            or received_currency != self.currency_id.name
        ):
            _logger.warning(
                "HiPay PaymentIntent %s amount %s %s does not match transaction %s (%s %s)",
                payment_intent.get('id'), payment_intent.get('amount'), received_currency,
                self.reference, expected_amount, self.currency_id.name,
            )
            self._set_error("HiPay: " + _("The paid amount does not match the order amount."))
            return

        status = payment_intent.get('status')
        if status in const.PAYMENT_STATUS_MAPPING['done']:
            self._set_done()
        elif status in const.PAYMENT_STATUS_MAPPING['authorized']:
            self._set_authorized()
        elif status in const.PAYMENT_STATUS_MAPPING['pending']:
            self._set_pending()
        elif status in const.PAYMENT_STATUS_MAPPING['waiting']:
            # Not paid yet. A declined attempt also lands here: the buyer can retry on the same
            # checkout page, so it is not final. The webhook or the next return settles it.
            error = payment_intent.get('last_payment_error') or {}
            _logger.info(
                "HiPay PaymentIntent %s for transaction %s is not paid yet (%s%s)",
                payment_intent.get('id'), self.reference, status,
                f", last error {error.get('code')}" if error else '',
            )
        elif status in const.PAYMENT_STATUS_MAPPING['cancel']:
            self._set_canceled("HiPay: " + _("The payment was canceled."))
        else:
            _logger.warning(
                "Received data with invalid payment status (%s) for transaction %s",
                status, self.reference,
            )
            self._set_error("HiPay: " + _("Received data with invalid payment status: %s", status))

    # === REFUNDS === #

    def _send_refund_request(self, amount_to_refund=None):
        """ Override of `payment` to send a refund request to HiPay.

        Note: self.ensure_one()

        :param float amount_to_refund: The amount to refund.
        :return: The refund transaction created to process the refund request.
        :rtype: recordset of `payment.transaction`
        """
        refund_tx = super()._send_refund_request(amount_to_refund=amount_to_refund)
        if self.provider_code != 'hipay':
            return refund_tx

        # Touch the source transaction row. A `charge.refunded` webhook for this refund locks the
        # same row, so it waits for this request to commit and then sees the refund transaction
        # created here instead of creating a second one.
        self.env.cr.execute(
            'UPDATE payment_transaction SET write_date = write_date WHERE id = %s', [self.id]
        )

        amount_minor = self._hipay_to_minor_units(-refund_tx.amount, refund_tx.currency_id)
        refund = self.provider_id._hipay_make_request(
            '/v1/refunds',
            payload={
                'payment_intent': self.provider_reference,
                'amount': amount_minor,
                'reason': 'requested_by_customer',
                'metadata': {
                    'odoo_reference': refund_tx.reference,
                    'source': 'odoo',
                },
            },
            idempotency_key=refund_tx._hipay_idempotency_key(),
        )
        _logger.info(
            "HiPay refund %s requested for transaction %s:\n%s",
            refund.get('id'), self.reference, pprint.pformat(refund),
        )
        refund_tx.provider_reference = refund.get('id')
        refund_tx._handle_notification_data('hipay', {'refund': refund})
        return refund_tx

    def _hipay_process_refund(self, refund):
        """ Update a refund transaction from a HiPay refund object. """
        if not refund:
            raise ValidationError("HiPay: " + _("Received data with missing refund."))
        if self.provider_reference and refund.get('id') != self.provider_reference:
            raise ValidationError("HiPay: " + _(
                "The refund %(received)s does not belong to transaction %(ref)s.",
                received=refund.get('id'), ref=self.reference,
            ))
        self.provider_reference = refund.get('id')
        status = refund.get('status')
        if status in const.REFUND_STATUS_MAPPING['done']:
            self._set_done()
        elif status in const.REFUND_STATUS_MAPPING['pending']:
            self._set_pending()
        elif status in const.REFUND_STATUS_MAPPING['cancel']:
            self._set_canceled("HiPay: " + _("The refund was canceled."))
        elif status in const.REFUND_STATUS_MAPPING['error']:
            self._set_error("HiPay: " + _("The refund failed."))
        else:
            self._set_error("HiPay: " + _("Received data with invalid refund status: %s", status))

    def _hipay_sync_refunds(self, charge):
        """ Create the refund transactions for refunds made outside Odoo (HiPay portal, API).

        Called for a `charge.refunded` event. Refunds that already have a transaction (including
        the ones Odoo itself requested) are skipped, so a repeated event changes nothing.

        Note: self.ensure_one()

        :param dict charge: The HiPay charge object of the event.
        :return: The refund transactions created.
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()
        # Serialize with `_send_refund_request` (see the comment there).
        self.env.cr.execute(
            'SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE', [self.id]
        )
        refunds = []
        params = {'charge': charge['id'], 'limit': 100}
        while True:
            page = self.provider_id._hipay_make_request('/v1/refunds', method='GET', params=params)
            refunds += page.get('data') or []
            if not page.get('has_more') or not page.get('data'):
                break
            params['starting_after'] = page['data'][-1]['id']

        known = set(self.child_transaction_ids.filtered(
            lambda t: t.operation == 'refund'
        ).mapped('provider_reference'))
        created = self.env['payment.transaction']
        for refund in refunds:
            if refund.get('id') in known or refund.get('status') not in ('succeeded', 'pending'):
                continue
            amount = self._hipay_to_major_units(refund['amount'], self.currency_id)
            refund_tx = self._create_child_transaction(
                amount, is_refund=True, provider_reference=refund['id']
            )
            refund_tx._handle_notification_data('hipay', {'refund': refund})
            created |= refund_tx
        return created
