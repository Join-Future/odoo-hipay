# Part of HiPay by Join Future. See LICENSE file for full copyright and licensing details.

# The production HiPay API. Test and live use the same host: the secret key prefix
# (hp_sk_test_ / hp_sk_live_) selects the mode on HiPay's side.
DEFAULT_API_URL = 'https://pay.joinfuture.ai'

# The currencies HiPay accepts, with their number of minor-unit decimal places.
# Mirrors CURRENCY_EXPONENTS / SUPPORTED_CURRENCIES in @hipay/shared. BHD, KWD, OMR
# and JOD have 3 decimals (1 BHD = 1000 fils), the others have 2.
CURRENCY_DECIMALS = {
    'BHD': 3,
    'KWD': 3,
    'OMR': 3,
    'JOD': 3,
    'SAR': 2,
    'AED': 2,
    'QAR': 2,
    'EGP': 2,
    'USD': 2,
}
SUPPORTED_CURRENCIES = tuple(CURRENCY_DECIMALS)

# The codes of the payment methods activated when the provider is enabled.
DEFAULT_PAYMENT_METHOD_CODES = {
    # Primary payment methods.
    'card',
    'benefit',
    # Brand payment methods.
    'visa',
    'mastercard',
}

# Odoo payment method code -> HiPay `payment_method_types` value. Every card brand
# (and any wallet linked by the merchant) goes through HiPay's card checkout, which
# also offers Apple Pay and Google Pay on supported devices.
PAYMENT_METHODS_MAPPING = {
    'benefit': 'benefit',
}
DEFAULT_HIPAY_METHOD = 'card'

# HiPay PaymentIntent statuses, grouped by the Odoo transaction state they lead to.
# See docs/integration/overview.md (lifecycle) in the HiPay repository.
PAYMENT_STATUS_MAPPING = {
    'pending': ('processing',),
    'waiting': ('requires_payment_method', 'requires_confirmation', 'requires_action'),
    'authorized': ('requires_capture',),
    'done': ('succeeded',),
    'cancel': ('canceled',),
}

# HiPay refund statuses.
REFUND_STATUS_MAPPING = {
    'pending': ('pending',),
    'done': ('succeeded',),
    'cancel': ('canceled',),
    'error': ('failed',),
}

# Webhook events this module acts on. Others are acknowledged and ignored.
HANDLED_WEBHOOK_EVENTS = (
    'payment_intent.succeeded',
    'payment_intent.processing',
    'payment_intent.payment_failed',
    'payment_intent.canceled',
    'payment_intent.amount_capturable_updated',
    'charge.refunded',
)

# Maximum age, in seconds, of a webhook signature timestamp (either direction).
# Same default as constructEvent() in @hipay/hipay-node.
WEBHOOK_TOLERANCE = 300

# Header carrying the webhook signature: "t=<unix seconds>,v1=<hex hmac-sha256>".
SIGNATURE_HEADER = 'HiPay-Signature'

# Keys of the API responses that must never reach the logs.
SENSITIVE_KEYS = {'client_secret'}
