"""Transactional checkout reminder rendering; no delivery, tracking, or account access."""
from __future__ import annotations

from email.message import EmailMessage
from html import escape
import re
from urllib.parse import quote, urlsplit

COPY = {
    'uk': {
        'subject': 'Ваше оформлення в Soul Cuts ще не завершено',
        'heading': 'Оформлення ще не завершено',
        'intro': 'Ви почали оформлення в Soul Cuts, але ще не завершили його. Збережені товари нижче — ви можете повернутися та перевірити їх перед замовленням.',
        'items': 'Ваші збережені товари', 'quantity': 'Кількість',
        'cta': 'Продовжити оформлення',
        'support': 'Потрібна допомога? Напишіть нам:',
        'footer': 'Це одноразове сервісне нагадування про ваше незавершене оформлення в Soul Cuts. Замовлення ще не підтверджено. Ціни та наявність перевіряються під час повернення. Посилання діє обмежений час.',
        'fallback': 'Товар', 'no_image': 'Зображення відсутнє',
    },
    'en': {
        'subject': 'Your Soul Cuts checkout is unfinished',
        'heading': 'Your checkout is unfinished',
        'intro': 'You started checkout at Soul Cuts but have not finished it yet. Your saved items are below, ready for you to review before placing an order.',
        'items': 'Your saved items', 'quantity': 'Quantity',
        'cta': 'Continue checkout', 'support': 'Need help? Contact us:',
        'footer': 'This is a one-time service reminder about your unfinished Soul Cuts checkout. Your order has not been confirmed. Prices and availability are checked when you return. The link expires.',
        'fallback': 'Product', 'no_image': 'Image unavailable',
    },
}


def _text(value, limit=1000):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or '')).strip()[:limit]


def allowed_https_url(value, allowed_origins):
    """Exact origin allowlist; credentials, protocol-relative URLs and controls fail closed."""
    if not isinstance(value, str) or re.search(r'[\x00-\x20\x7f\\]', value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return None
        origin = f'https://{parsed.hostname.lower()}' + (f':{parsed.port}' if parsed.port not in (None, 443) else '')
        permitted = set()
        for item in allowed_origins:
            candidate = urlsplit(item)
            if candidate.scheme == 'https' and candidate.hostname and not candidate.username and not candidate.password and candidate.path in ('', '/') and not candidate.query and not candidate.fragment:
                permitted.add(f'https://{candidate.hostname.lower()}' + (f':{candidate.port}' if candidate.port not in (None, 443) else ''))
        return value if origin in permitted else None
    except (TypeError, ValueError):
        return None


def build_checkout_reminder_message(locale, items, recovery_url, logo_url, support_email, allowed_origins):
    """Build multipart/alternative. Caller supplies verified settings and recipient separately.

    Product names/variants are presentation only; recovery must revalidate the catalog.
    No names, recipient address, prices, analytics or transport metadata are embedded.
    """
    if locale not in COPY:
        raise ValueError('Unsupported checkout locale')
    recovery_url = allowed_https_url(recovery_url, allowed_origins)
    if not recovery_url:
        raise ValueError('Recovery origin is not allowed')
    if not isinstance(support_email, str) or not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', support_email) or len(support_email) > 254:
        raise ValueError('A valid configured support address is required')
    c = COPY[locale]
    rows, text_rows = [], []
    for item in items:
        quantity = item.get('quantity')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 999:
            raise ValueError('Invalid checkout item quantity')
        name = _text(item.get('name'), 255) or c['fallback']
        variant = _text(item.get('variant'), 255)
        image = allowed_https_url(item.get('image_url'), allowed_origins)
        image_html = (f'<img src="{escape(image, quote=True)}" width="240" alt="{escape(name, quote=True)}" style="display:block;width:100%;max-width:240px;height:auto;border:0;">' if image else f'<p style="color:#737373;font-size:14px;">{c["no_image"]}</p>')
        rows.append(f'''<tr><td class="stack" width="240" valign="middle" style="width:240px;padding:15px 0;">{image_html}</td><td class="stack item-copy" width="240" valign="middle" style="width:240px;padding:15px 0;word-break:break-word;overflow-wrap:anywhere;"><div class="item-inner" style="padding-left:30px;"><p style="margin:0 0 8px;font-size:16px;line-height:1.5;">{escape(name)}</p>{f'<p style="margin:0 0 8px;font-size:14px;line-height:1.5;">{escape(variant)}</p>' if variant else ''}<p style="margin:0;font-size:14px;line-height:1.5;color:#a1a1a1;">{c['quantity']}: {quantity}</p></div></td></tr>''')
        text_rows.append(f'{name}' + (f' — {variant}' if variant else '') + f"\n{c['quantity']}: {quantity}")
    if not rows:
        raise ValueError('Checkout items are required')
    logo = allowed_https_url(logo_url, allowed_origins)
    brand = f'<img src="{escape(logo, quote=True)}" width="110" alt="Soul Cuts" style="display:block;width:110px;max-width:100%;height:auto;border:0;margin:0 auto;">' if logo else '<strong style="font-size:24px;">Soul Cuts</strong>'
    html = f'''<!doctype html>
<html lang="{locale}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="x-apple-disable-message-reformatting"><meta name="format-detection" content="telephone=no,address=no,email=no,date=no,url=no"><title>{c['subject']}</title>
<!--[if mso]><style>body,table,td,a{{font-family:Arial,sans-serif!important;}}table{{border-collapse:collapse;}}</style><![endif]-->
<style>body{{margin:0!important;padding:0!important;width:100%!important;}}table{{border-spacing:0;mso-table-lspace:0pt;mso-table-rspace:0pt;}}a[x-apple-data-detectors]{{color:inherit!important;text-decoration:none!important;}}@media only screen and (max-width:480px){{.container{{width:100%!important;}}.inset{{padding-left:15px!important;padding-right:15px!important;}}.stack{{display:block!important;width:100%!important;box-sizing:border-box!important;}}.item-copy{{padding:0 0 24px!important;}}.item-inner{{padding-left:0!important;}}.stack img{{margin:0 auto!important;}}.cta{{display:block!important;box-sizing:border-box!important;width:100%!important;}}.heading{{font-size:32px!important;}}}}</style></head>
<body style="background:#fafafa;color:#0a0a0a;font-family:'Helvetica Neue',Helvetica,-apple-system,BlinkMacSystemFont,Arial,sans-serif;">
<div style="display:none;font-size:1px;color:#fafafa;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{c['subject']}</div>
<table role="presentation" width="100%" style="width:100%;background:#fafafa;"><tr><td align="center" style="padding:24px 0;">
<!--[if mso]><table role="presentation" width="600" align="center"><tr><td><![endif]-->
<table role="presentation" class="container" width="600" style="width:100%;max-width:600px;background:#ffffff;">
<tr><td align="center" class="inset" style="padding:40px 60px 24px;">{brand}</td></tr>
<tr><td class="inset" align="center" style="padding:0 60px 24px;"><h1 class="heading" style="margin:0;font-size:39px;line-height:1.205;font-weight:700;">{c['heading']}</h1></td></tr>
<tr><td class="inset" style="padding:0 60px 28px;"><p style="margin:0;font-size:16px;line-height:1.5;">{c['intro']}</p></td></tr>
<tr><td class="inset" style="padding:0 60px 12px;"><h2 style="margin:0;font-size:20px;line-height:1.4;">{c['items']}</h2></td></tr>
<tr><td class="inset" style="padding:0 60px 20px;"><table role="presentation" width="100%" style="width:100%;table-layout:fixed;">{''.join(rows)}</table></td></tr>
<tr><td class="inset" align="center" style="padding:0 60px 28px;"><a class="cta" href="{escape(recovery_url, quote=True)}" target="_blank" rel="noreferrer noopener" style="display:inline-block;background:#000000;color:#ffffff;text-decoration:none;font-size:16px;font-weight:700;line-height:24px;border-radius:12px;padding:14px 28px;mso-padding-alt:14px 28px;text-align:center;">{c['cta']}</a></td></tr>
<tr><td class="inset" style="padding:0 60px 32px;"><p style="margin:0;font-size:16px;line-height:1.5;">{c['support']} <a href="mailto:{escape(quote(support_email, safe="@."), quote=True)}" style="color:#0a0a0a;word-break:break-word;">{escape(support_email)}</a></p></td></tr>
<tr><td class="inset" style="padding:28px 60px;background:#000000;color:#ffffff;"><p style="margin:0 0 12px;font-size:16px;font-weight:700;">Soul Cuts</p><p style="margin:0;font-size:12px;line-height:1.6;">{c['footer']}</p></td></tr>
</table><!--[if mso]></td></tr></table><![endif]-->
</td></tr></table></body></html>'''
    message = EmailMessage()
    message['Subject'] = c['subject']
    message.set_content(f"Soul Cuts\n\n{c['heading']}\n\n{c['intro']}\n\n{c['items']}\n\n" + '\n\n'.join(text_rows) + f"\n\n{c['cta']}: {recovery_url}\n\n{c['support']} {support_email}\n\n{c['footer']}\n")
    message.add_alternative(html, subtype='html')
    return message
