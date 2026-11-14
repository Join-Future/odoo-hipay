# HiPay by Join Future for Odoo

`payment_joinfuture_hipay` is an Odoo 18 payment provider for **HiPay by Join Future**, the
payment gateway for Bahrain and the GCC. Customers pay on the HiPay hosted
checkout (cards with 3-D Secure, BENEFIT, Apple Pay, Google Pay) and come back to
Odoo, which confirms the payment with the HiPay API.

- Author: Join Future. License: LGPL-3. Price: free.
- Depends only on `payment`. Works with `website_sale`, the customer portal,
  invoice payment links and `account_payment` refunds.

## How it works

1. When the customer clicks **Pay now**, Odoo creates a HiPay PaymentIntent with
   the secret key (`POST /v1/payment_intents`):
   - `amount` in minor units of the currency (3 decimals for BHD, KWD, OMR and
     JOD, 2 for SAR, AED, QAR, EGP and USD);
   - `payment_method_types` = `["card"]` or `["benefit"]`, from the method the
     customer picked;
   - `return_url` = `/payment/hipay/return?reference=<tx reference>`;
   - `metadata.odoo_reference` and an `Idempotency-Key` made of the database,
     the transaction id and its reference, so a retry never creates a second
     payment;
   - `line_items`, `vat_amount` and `vat_rate_bps` from the sale order (or the
     invoice), so the checkout shows each item with its image, the delivery line
     and the VAT row. Each line carries its unit price excluding tax after
     discounts, and `sum(lines) + vat_amount == amount` holds to the fil. A line
     whose total does not split into whole fils per unit is sent as one line
     named "Item x 3". Negative lines (promotions, deposits) are left out, and
     the checkout then shows the lines with an "includes VAT" note. A partial
     payment of a document sends no lines.
2. The customer is sent to `{API URL}/hosted/checkout/<pi_id>`.
3. On the way back, `/payment/hipay/return` ignores the `status` in the URL and
   reads the PaymentIntent again with `GET /v1/payment_intents/<id>`. Only the
   PaymentIntent created for that transaction, for exactly that amount and
   currency, can confirm it.
4. The webhook `/payment/hipay/webhook` checks the `HiPay-Signature` header
   (`t=<unix>,v1=<hex>`, HMAC-SHA256 of `"{t}.{raw body}"` with the `whsec_`
   secret, compared in constant time, refused when `t` is more than 300 seconds
   away) before it reads anything. It then re-reads the PaymentIntent from the
   API. `charge.refunded` adds refunds made outside Odoo (for example in the
   HiPay portal) as refund transactions, each one once.
5. Refunds from Odoo call `POST /v1/refunds` with the amount in minor units and
   an idempotency key. Full and partial refunds are supported.

| HiPay PaymentIntent status | Odoo transaction |
|---|---|
| `succeeded` | Confirmed |
| `processing` | Pending |
| `requires_capture` | Authorized |
| `requires_payment_method`, `requires_confirmation`, `requires_action` | Unchanged (a declined card can be retried on the same checkout) |
| `canceled` | Canceled |

## Install

1. Copy the `payment_joinfuture_hipay` folder into your Odoo addons path (or install it from
   the Odoo Apps store), restart Odoo, then **Apps > Update Apps List**.
2. Install **Payment Provider: HiPay by Join Future**.

## Configure

1. Get a HiPay merchant account from Join Future
   ([pay.joinfuture.ai](https://pay.joinfuture.ai)). You get a test secret key
   (`hp_sk_test_...`) at once and a live key (`hp_sk_live_...`) after onboarding.
2. In Odoo open **Invoicing (or Website) > Configuration > Payment Providers >
   HiPay by Join Future**.
3. **Credentials** tab:
   - **HiPay API URL**: keep `https://pay.joinfuture.ai`.
   - **Test Secret Key** and **Live Secret Key**.
   - **Test / Live Webhook Signing Secret**: in the HiPay portal, add a webhook
     endpoint for each mode with the URL `https://<your odoo>/payment/hipay/webhook`
     and the events `payment_intent.succeeded`, `payment_intent.payment_failed`,
     `payment_intent.canceled`, `payment_intent.processing` and `charge.refunded`.
     Copy the `whsec_...` secret it shows once.
4. Set **State** to **Test Mode** (uses the test key) or **Enabled** (uses the
   live key). Odoo refuses a state whose key is missing or has the wrong prefix.
5. **Configuration** tab: check the payment methods (Card with Visa and
   Mastercard, BENEFIT), the journal, and publish the provider.

Test card in test mode: `5123 4567 8901 2346`, expiry `01/39`, CVV `100`.

Your Odoo must be reachable on a public `https` address in live mode: HiPay
refuses `localhost` and private addresses as a return URL or webhook URL.

## Tests

The tests mock every HTTP call to HiPay:

```bash
odoo -d <test db> -i payment_joinfuture_hipay --test-enable --test-tags /payment_joinfuture_hipay --stop-after-init
```

They cover PaymentIntent creation and the redirect form, 3-decimal amounts,
line items with VAT (3-decimal sums, the delivery line, quantities that do not
split, no tax, mixed rates, negative lines), the return route (verified with the
API, idempotent, rejects another PaymentIntent, ignores the status in the URL,
amount mismatch, cancel), the webhook signature (valid, wrong secret, tampered
body, stale and future timestamps, missing header), and full, partial and failed
refunds including refunds made outside Odoo. With `sale` installed, a real sale
order with 10% VAT is also checked.

## Odoo 17

The module is written for Odoo 18. For Odoo 17:

- set `'version': '17.0.1.0.0'` in `__manifest__.py`;
- the payment flow API is the same (`_get_specific_rendering_values`,
  `_get_tx_from_notification_data`, `_process_notification_data`,
  `_send_refund_request`, `payment.method` records), and the
  `invisible="..."` / `required="..."` view syntax is already 17 syntax;
- in Odoo 17 the sale order line tax field is also `tax_id`, so line items work
  unchanged;
- check that the `payment.payment_method_benefit` and `payment_method_card`
  records referenced in `data/payment_provider_data.xml` exist in your 17 build;
- only Odoo 18 was tested here: run the tests on a 17 database before publishing
  a `17.0` branch.

Odoo 19 reworked the transaction processing methods of the payment API and needs
its own port.

## Publish on the Odoo Apps store

apps.odoo.com reads modules from a **public git repository**, one branch per Odoo
version (`18.0` here), with the module folder at the root of the branch. This
repository keeps the module under `integrations/odoo/`, so export it first:

```bash
integrations/odoo/export_module_repo.sh ./build/odoo-hipay git@github.com:<org>/odoo-hipay.git
```

The script builds a fresh repository with `payment_joinfuture_hipay/`, `LICENSE` and
`README.md` at the root on branch `18.0`, and pushes it when a remote is given.
Then, signed in to odoo.com:

1. Go to **apps.odoo.com > Upload / Manage your apps** (the "Publish" page).
2. Add the repository with its SSH URL and branch, for example
   `ssh://git@github.com/<org>/odoo-hipay.git#18.0`. If the repository were
   private, the page gives a deploy key to add on GitHub, but a public repository
   is the simplest.
3. The store scans the branch and lists **Payment Provider: HiPay by Join
   Future** with the description from `static/description/index.html`, the icon
   and the banner from the manifest `images`.
4. The module is free (`'price': 0`), so no payout account is needed. A paid
   module would need the vendor's bank details on odoo.com.
5. After each change, bump `version` in `__manifest__.py`, run the export script
   again, and the store picks up the new commit.
