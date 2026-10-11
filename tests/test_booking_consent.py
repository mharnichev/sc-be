"""Explicit website consent payload compatibility."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.booking import PublicBookingCreate


def booking_payload(**changes):
    return dict(master_id=1, service_id=1, customer_name="Synthetic customer",
                customer_phone="+380500000001", start_at=datetime.now(timezone.utc), **changes)


def test_legacy_public_booking_defaults_to_no_marketing_consent():
    assert PublicBookingCreate(**booking_payload()).sms_consent is False


@pytest.mark.parametrize("consent", [False, True])
def test_public_booking_accepts_explicit_boolean_consent(consent):
    assert PublicBookingCreate(**booking_payload(sms_consent=consent)).sms_consent is consent


@pytest.mark.parametrize("consent", [None, "true", "false", 0, 1])
def test_public_booking_rejects_non_boolean_consent(consent):
    with pytest.raises(ValidationError):
        PublicBookingCreate(**booking_payload(sms_consent=consent))
