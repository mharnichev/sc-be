"""Supplied null and promotion date contracts, without database/provider work."""
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api.v1.routes import bookings, messaging, promotions
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_master
from app.schemas.booking import (
    AdminBookingUpdate,
    BarberServiceUpdate,
    BaseServiceUpdate,
    sync_service_text_data,
)
from app.schemas.messaging import ClientCommunicationPreferenceUpdate
from app.schemas.promotion import PromotionCreate, PromotionUpdate
from app.services.promotion import PromotionService


PROMOTION_REQUIRED_FIELDS = (
    "code", "name_uk", "name_en", "discount_type", "discount_percent",
    "application_mode", "eligibility_type", "applies_to_all_masters",
    "applies_to_all_services", "recipient_offer_only", "is_public", "is_active",
)
SERVICE_REQUIRED_FIELDS = ("name", "duration_minutes", "price", "is_active")
PREFERENCE_REQUIRED_FIELDS = (
    "marketing_consent", "transactional_consent", "do_not_contact", "repeat_booking_opt_out",
)
REQUIRED_UPDATES = (
    (PromotionUpdate, PROMOTION_REQUIRED_FIELDS),
    (BaseServiceUpdate, SERVICE_REQUIRED_FIELDS),
    (BarberServiceUpdate, SERVICE_REQUIRED_FIELDS),
    (ClientCommunicationPreferenceUpdate, PREFERENCE_REQUIRED_FIELDS),
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("schema,field", [
    (schema, field) for schema, fields in REQUIRED_UPDATES for field in fields
])
def test_explicit_null_required_update_field_is_rejected(schema, field):
    with pytest.raises(ValidationError):
        schema.model_validate({field: None})


@pytest.mark.parametrize("schema", [schema for schema, _ in REQUIRED_UPDATES])
def test_omitted_update_fields_are_not_written(schema):
    assert schema().model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize("schema,fields", [
    (PromotionUpdate, ("description_uk", "description_en", "starts_at", "ends_at", "inactive_days")),
    (BaseServiceUpdate, ("title_uk", "title_en", "description", "description_uk", "description_en")),
    (BarberServiceUpdate, ("title_uk", "title_en", "description", "description_uk", "description_en", "base_service_id")),
    (ClientCommunicationPreferenceUpdate, ("telegram_chat_id", "preferred_language", "opt_out_reason")),
])
def test_nullable_update_fields_can_still_clear(schema, fields):
    data = dict.fromkeys(fields)
    assert schema.model_validate(data).model_dump(exclude_unset=True) == data


@pytest.mark.parametrize("schema", [BaseServiceUpdate, BarberServiceUpdate])
def test_service_zero_price_and_false_activity_are_preserved(schema):
    data = {"price": 0, "is_active": False}
    assert schema.model_validate(data).model_dump(exclude_unset=True) == data


@pytest.mark.parametrize("schema", [BaseServiceUpdate, BarberServiceUpdate])
@pytest.mark.parametrize("data", [
    {"title_uk": "Стрижка"},
    {"name": None, "title_uk": "Стрижка"},
    {"name": "Стрижка", "title_uk": None},
])
def test_service_title_synchronization_keeps_effective_name(schema, data):
    payload = schema.model_validate(data)
    written = sync_service_text_data(payload.model_dump(exclude_unset=True))
    assert written["name"] == "Стрижка"
    assert written["title_uk"] == "Стрижка"


def test_promotion_false_flags_and_code_normalization_are_preserved():
    data = {"recipient_offer_only": False, "is_public": False, "is_active": False}
    assert PromotionUpdate(**data).model_dump(exclude_unset=True) == data
    assert PromotionUpdate(code=" update15 ").code == "UPDATE15"


@pytest.mark.parametrize("consent", ["unknown", "opted_in", "opted_out"])
def test_preference_valid_enums_and_false_values_are_preserved(consent):
    data = {"marketing_consent": consent, "transactional_consent": consent,
            "do_not_contact": False, "repeat_booking_opt_out": False}
    assert ClientCommunicationPreferenceUpdate(**data).model_dump(mode="json", exclude_unset=True) == data


def test_admin_booking_explicit_null_still_removes_promotion():
    assert AdminBookingUpdate(promotion_code=None).model_dump(exclude_unset=True) == {"promotion_code": None}


class NoDatabaseWork:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"Invalid request reached database operation {name}")


HTTP_NULL_CASES = [
    ("PATCH", "/promotions/{promotion_id}", promotions.update_promotion, field)
    for field in PROMOTION_REQUIRED_FIELDS
] + [
    ("PATCH", path, endpoint, field)
    for path, endpoint in (
        ("/admin/services/{service_id}", bookings.admin_update_base_service),
        ("/barbers/{barber_id}/services/{service_id}", bookings.update_barber_service),
        ("/masters/me/services/{service_id}", bookings.update_my_service),
    ) for field in SERVICE_REQUIRED_FIELDS
] + [
    (method, "/messaging/customers/{customer_id}/preferences", endpoint, field)
    for method, endpoint in (
        ("PUT", messaging.update_customer_communication_preferences),
        ("PATCH", messaging.patch_customer_communication_preferences),
    ) for field in PREFERENCE_REQUIRED_FIELDS
]


@pytest.mark.anyio
@pytest.mark.parametrize("method,path,endpoint,field", HTTP_NULL_CASES)
async def test_http_null_rejected_before_database_work(method, path, endpoint, field):
    app = FastAPI()
    app.add_api_route(path, endpoint, methods=[method])
    session = NoDatabaseWork()

    async def session_override():
        return session

    app.dependency_overrides[get_db_session] = session_override
    app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=1, is_superuser=True)
    app.dependency_overrides[get_current_master] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://validation.test",
    ) as client:
        url = path.format(promotion_id=1, service_id=1, barber_id=1, customer_id=1)
        response = await client.request(method, url, json={field: None})
    assert response.status_code == 422, response.text
    assert session.calls == []


def promotion_payload(schema, starts_at, ends_at):
    data = {"starts_at": starts_at, "ends_at": ends_at}
    if schema is PromotionCreate:
        data.update(code="DATES15", name_uk="Dates offer", name_en="Dates offer", discount_percent=15)
    return schema.model_validate(data)


@pytest.mark.parametrize("schema", [PromotionCreate, PromotionUpdate])
@pytest.mark.parametrize("starts_at,ends_at", [
    ("2030-01-01T10:00:00", "2030-01-01T11:00:00"),
    ("2030-01-01T08:00:00Z", "2030-01-01T11:00:00+02:00"),
    ("2030-01-01T10:00:00+02:00", "2030-01-01T09:00:00Z"),
    ("2030-01-01T10:00:00", "2030-01-01T09:00:00Z"),
    ("2030-01-01T08:00:00Z", "2030-01-01T11:00:00"),
    ("2026-10-25T03:30:00+03:00", "2026-10-25T03:15:00+02:00"),
])
def test_ordered_promotion_bounds_use_naive_kyiv_policy(schema, starts_at, ends_at):
    payload = promotion_payload(schema, starts_at, ends_at)
    # Validation compares normalized operands without rewriting the supplied dates.
    assert payload.starts_at == datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    assert payload.ends_at == datetime.fromisoformat(ends_at.replace("Z", "+00:00"))


@pytest.mark.parametrize("schema", [PromotionCreate, PromotionUpdate])
@pytest.mark.parametrize("starts_at,ends_at", [
    ("2030-01-01T10:00:00", "2030-01-01T10:00:00"),
    ("2030-01-01T11:00:00", "2030-01-01T10:00:00"),
    ("2030-01-01T08:00:00Z", "2030-01-01T10:00:00+02:00"),
    ("2030-01-01T09:00:00Z", "2030-01-01T10:00:00+02:00"),
    ("2030-01-01T10:00:00", "2030-01-01T08:00:00Z"),
    ("2030-01-01T11:00:00", "2030-01-01T08:00:00Z"),
    ("2030-01-01T08:00:00Z", "2030-01-01T10:00:00"),
    ("2030-01-01T09:00:00Z", "2030-01-01T10:00:00"),
    ("2026-10-25T03:15:00+02:00", "2026-10-25T03:30:00+03:00"),
])
def test_equal_and_reversed_promotion_bounds_raise_validation_error(schema, starts_at, ends_at):
    with pytest.raises(ValidationError, match="ends_at must be after starts_at"):
        promotion_payload(schema, starts_at, ends_at)


@pytest.mark.parametrize("schema", [PromotionCreate, PromotionUpdate])
@pytest.mark.parametrize("starts_at,ends_at", [
    (None, None), (None, "2030-01-01T11:00:00"), ("2030-01-01T10:00:00Z", None),
])
def test_promotion_nullable_bounds_remain_valid(schema, starts_at, ends_at):
    payload = promotion_payload(schema, starts_at, ends_at)
    assert (payload.starts_at is None) == (starts_at is None)
    assert (payload.ends_at is None) == (ends_at is None)


@pytest.mark.parametrize("field,stored,submitted", [
    ("ends_at", "2030-01-01T08:00:00+00:00", "2030-01-01T11:00:00"),
    ("starts_at", "2030-01-01T11:00:00+02:00", "2030-01-01T10:00:00"),
    ("ends_at", "2030-01-01T10:00:00", "2030-01-01T09:00:00+00:00"),
    ("starts_at", "2030-01-01T11:00:00", "2030-01-01T08:00:00+00:00"),
])
def test_partial_promotion_patch_checks_omitted_counterpart(field, stored, submitted):
    counterpart = "starts_at" if field == "ends_at" else "ends_at"
    promotion = SimpleNamespace(eligibility_type="all_customers", application_mode="code", inactive_days=None,
                                starts_at=None, ends_at=None)
    setattr(promotion, counterpart, datetime.fromisoformat(stored))
    payload = PromotionUpdate.model_validate({field: submitted})
    result = PromotionService().complete_update_data(promotion, payload.model_dump(exclude_unset=True))
    assert result[field] == datetime.fromisoformat(submitted)
    assert counterpart not in result
    cleared = PromotionService().complete_update_data(promotion, PromotionUpdate(**{field: None}).model_dump(exclude_unset=True))
    assert cleared[field] is None


@pytest.mark.parametrize("field,stored,submitted", [
    ("ends_at", "2030-01-01T08:00:00+00:00", "2030-01-01T10:00:00"),
    ("starts_at", "2030-01-01T10:00:00+02:00", "2030-01-01T11:00:00"),
    ("ends_at", "2030-01-01T10:00:00", "2030-01-01T07:00:00+00:00"),
    ("starts_at", "2030-01-01T10:00:00", "2030-01-01T08:00:00+00:00"),
])
def test_invalid_partial_promotion_patch_keeps_existing_http_400(field, stored, submitted):
    counterpart = "starts_at" if field == "ends_at" else "ends_at"
    promotion = SimpleNamespace(eligibility_type="all_customers", application_mode="code", inactive_days=None,
                                starts_at=None, ends_at=None)
    setattr(promotion, counterpart, datetime.fromisoformat(stored))
    payload = PromotionUpdate.model_validate({field: submitted})
    with pytest.raises(HTTPException) as exc_info:
        PromotionService().complete_update_data(promotion, payload.model_dump(exclude_unset=True))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "ends_at must be after starts_at"


@pytest.mark.parametrize("schema", [PromotionCreate, PromotionUpdate])
@pytest.mark.parametrize("starts_at,ends_at,valid", [
    ("0001-01-01T00:00:00+02:00", "0001-01-01T01:00:00+02:00", True),
    ("0001-01-01T01:00:00+02:00", "0001-01-01T00:00:00+02:00", False),
    ("9999-12-31T23:00:00-02:00", "9999-12-31T23:30:00-02:00", True),
    ("9999-12-31T23:30:00-02:00", "9999-12-31T23:00:00-02:00", False),
])
def test_promotion_datetime_limits_keep_ordered_bounds_and_validate_reversed(schema, starts_at, ends_at, valid):
    data = {"starts_at": starts_at, "ends_at": ends_at}
    if schema is PromotionCreate:
        data.update(code="LIMIT", name_uk="Limit", name_en="Limit", discount_percent=10)
    if valid:
        result = schema.model_validate(data)
        assert result.starts_at.isoformat() == starts_at
        assert result.ends_at.isoformat() == ends_at
    else:
        with pytest.raises(ValidationError):
            schema.model_validate(data)
