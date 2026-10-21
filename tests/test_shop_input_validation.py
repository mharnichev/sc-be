"""Public request contracts reject malformed direct API inputs before services run."""
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.auth import CustomerOtpRequest, CustomerOtpVerifyRequest, PublicCustomerProfileUpdate
from app.schemas.feedback import FeedbackEmailRequest
from app.schemas.order import OrderCreate, PublicOrderCreate
from app.schemas.product import ProductReviewCreate, ProductReviewCommentCreate


def checkout(**changes):
    return PublicOrderCreate.model_validate({
        "firstName": "Олена", "lastName": "O’Neill", "phoneNumber": "+380671234567",
        "items": [{"productId": 1, "quantity": 1}], **changes,
    })


@pytest.mark.parametrize("field,value", [
    ("firstName", "A" * 81), ("lastName", "a@b"),
    ("firstName", "Synthetic 0"), ("lastName", "Synthetic 1"),
    ("customer_name", "<script>alert(1)</script>"),
    ("customer_phone", "12345"), ("phoneNumber", "+１２３４５６７８９０"),
    ("email", "a@example.com\r\nBcc: spam@example.com"),
    ("shippingCity", "a" * 161), ("buildingNumber", "a" * 21),
    ("shippingApartment", "1; DELETE"), ("shippingWarehouseNumber", "1" * 21), ("comment", "a" * 501),
    ("shippingPayloadJson", {"cityLabel": "a" * 161}),
])
def test_direct_checkout_invalid_fields(field, value):
    with pytest.raises(ValidationError):
        checkout(**{field: value})


@pytest.mark.parametrize("quantity", [True, "2", 1.5, 0, 1000])
def test_quantity_requires_bounded_integer(quantity):
    with pytest.raises(ValidationError):
        checkout(items=[{"productId": 1, "quantity": quantity}])


def test_item_count_bounded():
    with pytest.raises(ValidationError):
        checkout(items=[{"productId": 1, "quantity": 1}] * 101)


def test_checkout_sanitizes_stored_labels_and_comments():
    result = checkout(comment=" <b>Leave near door</b>\u202e\x00 ", shippingPayloadJson={"cityLabel": "<b>Київ</b>", "unknown": "ignored"})
    assert result.comment == "Leave near door"
    assert result.shipping_payload_json == {"cityLabel": "Київ"}
    assert result.resolved_customer_name == "Олена O’Neill"


def test_backoffice_order_contract_unchanged():
    assert OrderCreate(customer_name="Business 123", customer_phone="12345", items=[{"productId": 1, "quantity": 1000}]).items[0].quantity == 1000


@pytest.mark.parametrize("value", ["a" * 21, "visit https://a.ua https://b.ua https://c.ua", "   ", "<script>bad()</script>"])
def test_feedback_spam_or_empty_rejected(value):
    with pytest.raises(ValidationError):
        FeedbackEmailRequest(name="Олена", email="a@example.com", text=value)


def test_feedback_trimmed_before_minimum_check():
    with pytest.raises(ValidationError):
        FeedbackEmailRequest(name="   ", email="a@example.com", text="          ok          ")


@pytest.mark.parametrize("schema", [ProductReviewCreate, ProductReviewCommentCreate])
def test_review_sanitizes_and_validates_after_cleaning(schema):
    assert schema(rating=5, comment=" <b>Great product</b> ").comment == "Great product"
    with pytest.raises(ValidationError):
        schema(rating=5, comment=" <b>a</b> ")


def test_optional_review_can_remain_rating_only():
    assert ProductReviewCreate(rating=5).comment is None


@pytest.mark.parametrize("phone", ["123456789", "+1234567890123456", "+380<script>1</script>", "+１２３４５６７８９０", "+380671234567\n"])
def test_invalid_phone_rejected(phone):
    with pytest.raises(ValidationError):
        CustomerOtpRequest(phone=phone)


@pytest.mark.parametrize("code", ["1234", "1234567", "１２３４５６", "12345\n"])
def test_otp_exact_ascii_digits(code):
    with pytest.raises(ValidationError):
        CustomerOtpVerifyRequest(phone="+380671234567", otp_code=code)


def test_public_patch_preserves_null_omission_and_empty_email():
    assert PublicCustomerProfileUpdate().model_dump(exclude_unset=True) == {}
    assert PublicCustomerProfileUpdate(name=None, email="").model_dump(exclude_unset=True) == {"name": None, "email": None}
    assert PublicCustomerProfileUpdate(name="Олена", surname="O’Neill").name == "Олена"


@pytest.mark.parametrize("birthday", [date(1899, 12, 31), date.today() + timedelta(days=1), "2023-02-29"])
def test_birthday_limits(birthday):
    with pytest.raises(ValidationError):
        PublicCustomerProfileUpdate(birthday=birthday)


@pytest.mark.parametrize("text", [
    "&lt;script&gt;alert(1)&lt;/script&gt;Good text here",
    "&amp;lt;img src=x onerror=alert(1)&amp;gt;Good text here",
    "&amp;amp;lt;b&amp;amp;gt;Good text here&amp;amp;lt;/b&amp;amp;gt;",
    "&#38;#60;svg onload=alert(1)&#38;#62;&#38;#60;/svg&#38;#62;Good text here",
])
def test_nested_encoded_markup_never_recreated(text):
    result = FeedbackEmailRequest(name="Олена", email="a@example.com", text=text)
    assert "<" not in result.text and ">" not in result.text
    assert "alert" not in result.text


@pytest.mark.parametrize("name", ["---", "...", "’ -", "\u0301"])
def test_names_need_unicode_letter(name):
    with pytest.raises(ValidationError):
        checkout(firstName=name)
    with pytest.raises(ValidationError):
        PublicCustomerProfileUpdate(name=name)


def test_optional_profile_names_can_be_cleared():
    result = PublicCustomerProfileUpdate(name="", surname="   ")
    assert result.name == result.surname == ""


@pytest.mark.parametrize("birthday", [True, 0, 1704067200, 1704067200.0, "1704067200", "2024-01-01T00:00:00", datetime(2024, 1, 1)])
def test_birthday_rejects_coercible_non_dates(birthday):
    with pytest.raises(ValidationError):
        PublicCustomerProfileUpdate(birthday=birthday)


@pytest.mark.parametrize('email', [
    'Alice <other@example.org>', '<other@example.org>',
    '<b>alice</b>@example.org', 'alice<em></em>@example.org',
    '<script>victim</script> <other@example.org>',
    'Alice &lt;other@example.org&gt;',
])
def test_public_emails_reject_raw_display_names_without_recipient_rewrite(email):
    for validate in (
        lambda: FeedbackEmailRequest(name='Alice', email=email, text='Please help with this question.'),
        lambda: PublicCustomerProfileUpdate(email=email),
        lambda: checkout(email=email),
        lambda: checkout(customer_email=email),
    ):
        with pytest.raises(ValidationError):
            validate()


def test_public_bare_email_still_allows_surrounding_whitespace():
    assert PublicCustomerProfileUpdate(email=' alice@example.org ').email == 'alice@example.org'
