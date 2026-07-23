from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from app.api.v1.routes.reviews import ensure_review_admin, prevent_private_review_caching
from app.models.booking import BarberService, Booking, BookingStatus, Master
from app.models.customer import Customer
from app.models.master_review import MasterReview, MasterReviewStatus
from app.models.messaging import (
    Campaign,
    CampaignStatus,
    CampaignType,
    ClientCommunicationPreference,
    ConsentStatus,
    MessageChannel,
    MessageDeliveryStatus,
    MessageLog,
    MessagePurpose,
    MessageRecipient,
    MessageTemplate,
    ReviewPlatform,
    ReviewRequest,
    ReviewRequestStatus,
)
from app.schemas.review import ReviewRequestSettings, ReviewRequestSettingsUpdate, ReviewSubmission
from app.services.booking import KYIV_TZ
from app.services.master_reviews import (
    MAX_REVIEW_METRICS_RANGE_DAYS,
    MasterReviewService,
    generate_review_token,
    format_exclusion_rules,
    parse_exclusion_rules,
    review_metrics_period_bounds,
    review_token_hash,
    sanitize_review_comment,
)
from app.services.messaging import (
    MessageProvider,
    MessagingService,
    ProviderSendResult,
    _review_request_is_within_frequency_cap,
    _recipient_delivery_load_options,
    _try_acquire_review_scheduler_lock,
)


class FakeScalarResult:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object | None:
        return self.value


class FakeMetricsResult:
    def __init__(
        self,
        *,
        scalar: object | None = None,
        rows: list[tuple] | None = None,
        one: tuple | None = None,
        scalar_rows: list[object] | None = None,
    ) -> None:
        self.scalar = scalar
        self.rows = rows or []
        self.one_row = one
        self.scalar_rows = scalar_rows or []

    def scalar_one(self) -> object | None:
        return self.scalar

    def all(self) -> list[tuple]:
        return self.rows

    def one(self) -> tuple | None:
        return self.one_row

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: self.scalar_rows)


class MetricsSession:
    def __init__(self, results: list[FakeMetricsResult]) -> None:
        self.results = results
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeMetricsResult:
        self.statements.append(statement)
        return self.results.pop(0)


def test_private_review_responses_are_not_cached() -> None:
    response = Response()

    prevent_private_review_caching(response)

    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"


def test_review_metrics_period_bounds_are_inclusive_kyiv_dates() -> None:
    start, end = review_metrics_period_bounds(date(2026, 3, 28), date(2026, 3, 29))

    assert start == datetime(2026, 3, 28, 0, 0, tzinfo=KYIV_TZ)
    assert end == datetime(2026, 3, 30, 0, 0, tzinfo=KYIV_TZ)
    assert start.utcoffset() == timedelta(hours=2)
    assert end.utcoffset() == timedelta(hours=3)
    assert review_metrics_period_bounds(None, None) == (None, None)

    with pytest.raises(HTTPException) as incomplete:
        review_metrics_period_bounds(date(2026, 3, 28), None)
    assert incomplete.value.status_code == 422

    with pytest.raises(HTTPException) as oversized:
        review_metrics_period_bounds(
            date(2026, 1, 1),
            date(2026, 1, 1) + timedelta(days=MAX_REVIEW_METRICS_RANGE_DAYS),
        )
    assert oversized.value.status_code == 422


@pytest.mark.anyio
async def test_review_metrics_use_one_booking_cohort_and_bulk_master_ratings() -> None:
    master = Master(id=3, full_name="Андрій")
    session = MetricsSession(
        [
            FakeMetricsResult(scalar=2),
            FakeMetricsResult(
                rows=[
                    (ReviewRequestStatus.sent, 1),
                    (ReviewRequestStatus.failed, 1),
                ]
            ),
            FakeMetricsResult(one=(2, 1, 1)),
            FakeMetricsResult(
                one=(
                    2,
                    1,
                    Decimal("5.0"),
                    Decimal("2.0"),
                    1,
                )
            ),
            FakeMetricsResult(
                rows=[
                    (3, MasterReviewStatus.approved, 5, 1),
                    (3, MasterReviewStatus.pending, 1, 1),
                ]
            ),
            FakeMetricsResult(scalar_rows=[master]),
        ]
    )
    start, end = review_metrics_period_bounds(date(2026, 7, 1), date(2026, 7, 31))

    result = await MasterReviewService().metrics(
        session,  # type: ignore[arg-type]
        period_start=start,
        period_end=end,
        master_id=3,
    )

    assert result.date_from == date(2026, 7, 1)
    assert result.date_to == date(2026, 7, 31)
    assert result.eligible_completed_visits == 2
    assert result.review_form_opens is None
    assert result.average_rating_by_master[0].approved_average_rating == 5.0
    assert result.average_rating_by_master[0].pending_review_count == 1
    assert len(session.statements) == 6
    for statement in session.statements[:5]:
        sql = str(statement)
        assert "bookings.status" in sql
        assert "bookings.start_at" in sql
        assert "bookings.master_id" in sql


class FakeReviewSession:
    def __init__(self, request_item: ReviewRequest | None) -> None:
        self.request_item = request_item
        self.added: list[object] = []
        self.commits = 0
        self.rolled_back = False

    async def execute(self, _: object) -> FakeScalarResult:
        return FakeScalarResult(self.request_item)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for item in self.added:
            if isinstance(item, MasterReview) and item.id is None:
                item.id = 501

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rolled_back = True


class SequenceSession:
    def __init__(self, responses: list[object | None], booking: Booking) -> None:
        self.responses = responses
        self.booking = booking
        self.added: list[object] = []

    async def execute(self, _: object) -> FakeScalarResult:
        return FakeScalarResult(self.responses.pop(0))

    def add(self, value: object) -> None:
        self.added.append(value)


class CapturingProvider(MessageProvider):
    channel = MessageChannel.sms

    def __init__(self) -> None:
        self.body: str | None = None

    async def send_message(
        self,
        *,
        destination: str,
        body: str,
        reply_markup: dict | None = None,
    ) -> ProviderSendResult:
        self.body = body
        return ProviderSendResult(provider_message_id="provider-1", raw_response={"echo": body})


class AdvisoryLockSession:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.rolled_back = False

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, _: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one=lambda: self.acquired)

    async def rollback(self) -> None:
        self.rolled_back = True


def valid_request(*, booking_status: BookingStatus = BookingStatus.completed) -> ReviewRequest:
    now = datetime.now(KYIV_TZ)
    customer = Customer(id=2, phone="+380501112233", name="Олена")
    master = Master(id=3, full_name="Андрій")
    booking = Booking(
        id=4,
        master_id=master.id,
        service_id=9,
        customer_id=customer.id,
        customer_name="Олена",
        customer_phone=customer.phone,
        start_at=now - timedelta(hours=4),
        end_at=now - timedelta(hours=3),
        status=booking_status,
        completed_at=now - timedelta(hours=3) if booking_status == BookingStatus.completed else None,
    )
    booking.customer = customer
    booking.master = master
    request_item = ReviewRequest(
        id=10,
        campaign_id=11,
        appointment_id=booking.id,
        customer_id=customer.id,
        master_id=master.id,
        platform=ReviewPlatform.internal,
        review_url="/masters",
        token_hash=review_token_hash("valid-token-value-that-is-long-enough"),
        expires_at=now + timedelta(days=1),
        scheduled_at=now - timedelta(hours=2),
        sent_at=now - timedelta(hours=1),
        channel=MessageChannel.telegram,
        status=ReviewRequestStatus.sent,
    )
    request_item.appointment = booking
    request_item.master = master
    return request_item


def test_review_tokens_are_random_and_only_hashes_are_deterministic() -> None:
    first_token, first_hash = generate_review_token()
    second_token, second_hash = generate_review_token()

    assert first_token != second_token
    assert first_hash != second_hash
    assert first_hash == review_token_hash(first_token)
    assert first_token not in first_hash
    assert len(first_hash) == 64


def test_rating_rejects_bool_float_and_out_of_range_values() -> None:
    for invalid in (True, 1.0, "1", 0, 6):
        with pytest.raises(ValidationError):
            ReviewSubmission(rating=invalid)


def test_comment_is_plain_text_normalized_and_control_characters_are_removed() -> None:
    assert sanitize_review_comment("  Дуже\x00 добре!  ") == "Дуже добре!"
    assert sanitize_review_comment(" \n ") is None


@pytest.mark.anyio
async def test_low_rating_for_completed_booking_is_created_pending() -> None:
    request_item = valid_request()
    session = FakeReviewSession(request_item)

    response = await MasterReviewService().submit(
        session,
        "valid-token-value-that-is-long-enough",
        ReviewSubmission(rating=1, comment="Чесний відгук"),
    )

    review = next(item for item in session.added if isinstance(item, MasterReview))
    assert response.status == "pending"
    assert review.rating == 1
    assert review.status == MasterReviewStatus.pending
    assert request_item.status == ReviewRequestStatus.submitted
    assert request_item.review_id == review.id


@pytest.mark.anyio
async def test_non_completed_booking_cannot_submit_review_and_does_not_leak_reason() -> None:
    session = FakeReviewSession(valid_request(booking_status=BookingStatus.cancelled))

    with pytest.raises(HTTPException) as exc_info:
        await MasterReviewService().submit(
            session,
            "valid-token-value-that-is-long-enough",
            ReviewSubmission(rating=5),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Review request is unavailable"


@pytest.mark.anyio
async def test_token_is_single_use() -> None:
    request_item = valid_request()
    request_item.status = ReviewRequestStatus.submitted
    request_item.review_id = 88

    with pytest.raises(HTTPException) as exc_info:
        await MasterReviewService().submit(
            FakeReviewSession(request_item),
            "valid-token-value-that-is-long-enough",
            ReviewSubmission(rating=5),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_invalid_and_expired_tokens_return_privacy_safe_distinct_statuses() -> None:
    service = MasterReviewService()
    with pytest.raises(HTTPException) as invalid:
        await service.get_request_by_token(FakeReviewSession(None), "missing-token-value-that-is-long-enough")

    expired_request = valid_request()
    expired_request.expires_at = datetime.now(KYIV_TZ) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as expired:
        await service.get_request_by_token(
            FakeReviewSession(expired_request),
            "valid-token-value-that-is-long-enough",
        )

    assert (invalid.value.status_code, invalid.value.detail) == (404, "Review request is unavailable")
    assert (expired.value.status_code, expired.value.detail) == (410, "Review request is unavailable")
    assert expired_request.status == ReviewRequestStatus.expired


@pytest.mark.anyio
async def test_review_request_context_is_locale_aware_with_safe_fallbacks() -> None:
    request_item = valid_request()
    request_item.master.first_name_en = "Andrew"
    request_item.master.last_name_en = "Smith"
    service = BarberService(
        id=9,
        master_id=request_item.master_id,
        name="Haircut",
        title_uk="Чоловіча стрижка",
        title_en="Men's haircut",
        duration_minutes=60,
        price=900,
    )
    request_item.appointment.service = service

    english = await MasterReviewService().public_request_context(
        FakeReviewSession(request_item),
        "valid-token-value-that-is-long-enough",
        locale="en",
    )
    ukrainian = await MasterReviewService().public_request_context(
        FakeReviewSession(request_item),
        "valid-token-value-that-is-long-enough",
        locale="uk",
    )

    assert english.master_name == "Andrew Smith"
    assert english.service_names == ["Men's haircut"]
    assert ukrainian.master_name == "Андрій"
    assert ukrainian.service_names == ["Чоловіча стрижка"]


def test_quiet_hours_move_evening_schedule_to_next_morning() -> None:
    scheduled = datetime(2026, 7, 22, 22, 30, tzinfo=KYIV_TZ)

    adjusted = MessagingService.adjust_for_quiet_hours(
        scheduled,
        quiet_from="21:00",
        quiet_to="09:00",
    )

    assert adjusted == datetime(2026, 7, 23, 9, 0, tzinfo=KYIV_TZ)


def test_review_request_has_booking_level_unique_constraint() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in ReviewRequest.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("appointment_id",) in unique_columns
    review_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in MasterReview.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("booking_id",) in review_unique_columns


def test_review_request_defaults_to_sms_delivery() -> None:
    assert ReviewRequest.__table__.c.channel.default.arg == MessageChannel.sms


def test_review_request_frequency_cap_depends_on_whether_review_was_submitted() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=KYIV_TZ)
    request_item = valid_request()
    request_item.status = ReviewRequestStatus.sent
    request_item.created_at = now - timedelta(days=89)

    assert _review_request_is_within_frequency_cap(
        request_item,
        now=now,
        unanswered_days=90,
        submitted_days=270,
    )

    request_item.created_at = now - timedelta(days=90)
    assert not _review_request_is_within_frequency_cap(
        request_item,
        now=now,
        unanswered_days=90,
        submitted_days=270,
    )

    request_item.status = ReviewRequestStatus.submitted
    request_item.review_id = 501
    request_item.created_at = now - timedelta(days=300)
    request_item.reviewed_at = now - timedelta(days=269)
    assert _review_request_is_within_frequency_cap(
        request_item,
        now=now,
        unanswered_days=90,
        submitted_days=270,
    )

    request_item.reviewed_at = now - timedelta(days=270)
    assert not _review_request_is_within_frequency_cap(
        request_item,
        now=now,
        unanswered_days=90,
        submitted_days=270,
    )


def test_failed_review_request_does_not_consume_frequency_cap() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=KYIV_TZ)
    request_item = valid_request()
    request_item.status = ReviewRequestStatus.failed
    request_item.created_at = now - timedelta(days=1)

    assert not _review_request_is_within_frequency_cap(
        request_item,
        now=now,
        unanswered_days=90,
        submitted_days=270,
    )


def test_review_request_settings_enforce_one_request_and_separate_caps() -> None:
    current = ReviewRequestSettings(enabled=True, delay_minutes=120)

    assert current.primary_channel == "sms"
    assert current.sms_fallback_enabled is False
    assert current.frequency_cap_count == 1
    assert current.frequency_cap_days == 90
    assert current.submitted_frequency_cap_days == 270

    with pytest.raises(ValidationError):
        ReviewRequestSettingsUpdate(
            enabled=True,
            delay_minutes=120,
            primary_channel="sms",
            sms_fallback_enabled=False,
            quiet_hours_enabled=True,
            quiet_hours_from="21:00",
            quiet_hours_to="09:00",
            frequency_cap_count=2,
            frequency_cap_days=90,
        )

    with pytest.raises(ValidationError):
        ReviewRequestSettingsUpdate(
            enabled=True,
            delay_minutes=120,
            primary_channel="telegram",
            sms_fallback_enabled=False,
            quiet_hours_enabled=True,
            quiet_hours_from="21:00",
            quiet_hours_to="09:00",
            frequency_cap_count=1,
            frequency_cap_days=90,
        )


def test_review_campaign_requires_marketing_consent_under_existing_rules() -> None:
    allowed, reason = MessagingService().communication_allowed(None, MessagePurpose.review_request)

    assert allowed is False
    assert reason == "Client has no marketing consent"


@pytest.mark.anyio
async def test_review_scheduler_skips_iteration_when_another_worker_holds_lock() -> None:
    session = AdvisoryLockSession(acquired=False)

    acquired = await _try_acquire_review_scheduler_lock(session)  # type: ignore[arg-type]

    assert acquired is False
    assert session.rolled_back is True


def test_non_superuser_cannot_moderate() -> None:
    with pytest.raises(HTTPException) as exc_info:
        ensure_review_admin(SimpleNamespace(is_superuser=False))

    assert exc_info.value.status_code == 403


def test_backoffice_review_contract_routes_are_registered() -> None:
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/v1/backoffice/reviews/request-settings" in paths
    assert "/api/v1/backoffice/reviews/masters/statistics" in paths
    assert "/api/v1/backoffice/reviews/masters/me/statistics" in paths
    assert "/api/v1/backoffice/reviews/masters/{master_id}/statistics" in paths

    metrics_operation = app.openapi()["paths"]["/api/v1/backoffice/reviews/metrics"]["get"]
    assert {parameter["name"] for parameter in metrics_operation["parameters"]} == {
        "date_from",
        "date_to",
        "master_id",
    }
    assert "Europe/Kyiv" in metrics_operation["description"]


def test_review_exclusion_rules_round_trip_and_reject_unknown_rules() -> None:
    parsed = parse_exclusion_rules(["master_id:3", "service_id:9", "master_id:3"])

    assert parsed == {"master_ids": [3], "service_ids": [9]}
    assert format_exclusion_rules(parsed) == ["master_id:3", "service_id:9"]
    with pytest.raises(HTTPException) as exc_info:
        parse_exclusion_rules(["customer_opted_out"])
    assert exc_info.value.status_code == 422


def test_recipient_delivery_eager_loads_complete_booking_context() -> None:
    option_paths = {str(option.path) for option in _recipient_delivery_load_options()}

    assert any("MessageRecipient.appointment" in path and "Booking.master" in path for path in option_paths)
    assert any("MessageRecipient.appointment" in path and "Booking.service" in path for path in option_paths)
    assert any(
        "MessageRecipient.appointment" in path
        and "Booking.service_items" in path
        and "BookingServiceItem.service" in path
        for path in option_paths
    )


@pytest.mark.anyio
async def test_delivery_falls_back_to_sms_without_persisting_plaintext_token() -> None:
    now = datetime.now(KYIV_TZ)
    customer = Customer(id=2, phone="+380501112233", name="Олена")
    master = Master(id=3, full_name="Андрій")
    booking = Booking(
        id=4,
        master_id=master.id,
        service_id=9,
        customer_id=customer.id,
        customer_name="Олена",
        customer_phone=customer.phone,
        start_at=now - timedelta(hours=4),
        end_at=now - timedelta(hours=3),
        status=BookingStatus.completed,
        completed_at=now - timedelta(hours=3),
    )
    booking.customer = customer
    booking.master = master
    template = MessageTemplate(
        id=5,
        name="review",
        channel=MessageChannel.telegram,
        body="Review: {{review_link}}",
    )
    campaign = Campaign(
        id=6,
        name="reviews",
        type=CampaignType.post_visit_review_request,
        status=CampaignStatus.active,
        channel=MessageChannel.telegram,
        purpose=MessagePurpose.review_request,
        template=template,
    )
    recipient = MessageRecipient(
        id=7,
        campaign_id=campaign.id,
        customer_id=customer.id,
        appointment_id=booking.id,
        channel=MessageChannel.telegram,
        status=MessageDeliveryStatus.pending,
        idempotency_key="review:4",
        attempts=0,
    )
    recipient.campaign = campaign
    recipient.customer = customer
    recipient.appointment = booking
    request_item = ReviewRequest(
        id=8,
        campaign_id=campaign.id,
        appointment_id=booking.id,
        customer_id=customer.id,
        master_id=master.id,
        platform=ReviewPlatform.internal,
        review_url="/masters",
        scheduled_at=now,
        channel=MessageChannel.telegram,
        fallback_channel=MessageChannel.sms,
        status=ReviewRequestStatus.scheduled,
    )
    preference = ClientCommunicationPreference(
        customer_id=customer.id,
        marketing_consent=ConsentStatus.opted_in,
        transactional_consent=ConsentStatus.opted_in,
    )
    session = SequenceSession([request_item, preference, request_item], booking)
    sms = CapturingProvider()

    await MessagingService({MessageChannel.sms: sms}).send_recipient(session, recipient)

    assert recipient.channel == MessageChannel.sms
    assert recipient.rendered_message is None
    assert request_item.status == ReviewRequestStatus.sent
    assert request_item.token_hash is not None
    assert sms.body is not None
    assert "/masters#" in sms.body
    token = sms.body.rsplit("#", maxsplit=1)[-1]
    assert request_item.token_hash == review_token_hash(token)
    log = next(item for item in session.added if isinstance(item, MessageLog))
    assert token not in str(log.provider_response)
