# Part of HiPay by Join Future. See LICENSE file for full copyright and licensing details.

{
    'name': "Payment Provider: HiPay by Join Future",
    'version': '18.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "Accept cards, BENEFIT, Apple Pay and Google Pay in Bahrain and the GCC with HiPay.",
    'description': " ",  # Non-empty string to avoid loading the README file.
    'author': "Join Future",
    'maintainer': "Join Future",
    'website': "https://pay.joinfuture.ai",
    'support': "hipay@joinfuture.ai",
    'license': 'LGPL-3',
    'price': 0,
    'currency': 'USD',
    'depends': ['payment'],
    'data': [
        'views/payment_joinfuture_hipay_templates.xml',
        'views/payment_provider_views.xml',

        'data/payment_provider_data.xml',
    ],
    'images': [
        'static/description/banner.png',
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}
