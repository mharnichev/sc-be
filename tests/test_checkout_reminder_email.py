from email import policy
from email.parser import BytesParser
import pytest
from app.services.checkout_reminder_email import build_checkout_reminder_message, allowed_https_url

ORIGINS = ['https://shop.example.test', 'https://assets.example.test']


def build(locale='en', **kwargs):
    args = dict(locale=locale, items=[{'name': 'Synthetic product <One>', 'variant': '250 ml', 'quantity': 2, 'image_url': 'https://assets.example.test/product.webp'}], recovery_url='https://shop.example.test/checkout/recover#token=synthetic-only', logo_url='https://shop.example.test/soulcuts/sc-logo-name-dark.webp', support_email='support@example.test', allowed_origins=ORIGINS)
    return build_checkout_reminder_message(**(args | kwargs))


@pytest.mark.parametrize('locale,cta', [('uk','Продовжити оформлення'), ('en','Continue checkout')])
def test_multipart_and_locales(locale, cta):
    message = BytesParser(policy=policy.default).parsebytes(build(locale).as_bytes())
    assert message.get_content_type() == 'multipart/alternative'
    assert message['To'] is None and message['From'] is None
    plain, html = (part.get_content() for part in message.iter_parts())
    assert cta in plain and cta in html
    assert '<html lang="' + locale + '"' in html
    assert 'Synthetic product &lt;One&gt;' in html
    assert 'Synthetic product <One>' in plain
    assert '250 ml' in html and '250 ml' in plain
    assert 'max-width:480px' in html and 'max-width:600px' in html
    assert html.count('class="cta"') == 1
    assert 'tracking' not in html and 'facebook' not in html
    assert 'confirmed' in plain if locale == 'en' else 'підтверджено' in plain
    assert 'one-time service reminder' in plain if locale == 'en' else 'одноразове сервісне нагадування' in plain
    for inaccurate_claim in ('You asked', 'you requested', 'Ви попросили', 'ви запросили'):
        assert inaccurate_claim not in plain and inaccurate_claim not in html


@pytest.mark.parametrize('url', ['http://shop.example.test/path', '//shop.example.test/path', 'https://shop.example.test.evil.test/path', 'https://user@shop.example.test/path', 'javascript:alert(1)', 'https://shop.example.test\\@evil.test', 'https://shop.example.test/path\nInjected', 'https://shop.example.test:444/path', 'https://shop.example.test:bad/path'])
def test_reject_unsafe_origins(url):
    assert allowed_https_url(url, ORIGINS) is None
    with pytest.raises(ValueError):
        build(recovery_url=url)


@pytest.mark.parametrize('support', ['a@example.test\r\nBcc: victim@example.test', 'a@example.test?subject=spam', '', 'javascript:alert(1)'])
def test_support_validation(support):
    with pytest.raises(ValueError):
        build(support_email=support)


def test_missing_images_names_long_names_and_escaping():
    html = build(items=[{'quantity': 1}, {'name': '<script>alert(1)</script>' * 50, 'variant': '\"><img src=x onerror=alert(1)>', 'quantity': 999, 'image_url': 'https://evil.test/pixel'}], logo_url='https://evil.test/logo').get_body(('html',)).get_content()
    assert 'Image unavailable' in html and 'Product' in html
    assert '<script>' not in html and '<img src=x' not in html
    assert 'evil.test' not in html
    assert 'Soul Cuts</strong>' in html
    assert 'overflow-wrap:anywhere' in html


@pytest.mark.parametrize('items', [[], [{'quantity': 0}], [{'quantity': -1}], [{'quantity': True}], [{'quantity': 1000}], [{'quantity': '1'}]])
def test_invalid_items(items):
    with pytest.raises(ValueError):
        build(items=items)


def test_locale_fails_closed():
    with pytest.raises(ValueError):
        build('ru')


def test_support_mailto_encodes_valid_special_local_part():
    html = build(support_email='help?team@example.test').get_body(('html',)).get_content()
    assert 'href="mailto:help%3Fteam@example.test"' in html
