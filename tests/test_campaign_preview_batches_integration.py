"""Preview wire goldens and work counters on an isolated, read-only PG view.

Fixed-clock cases characterize the existing response. The moving-clock case
requires all aggregate batches to honor the response's single evaluated_at.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, func, select, text

from app.models.booking import Booking, BookingStatus
from app.models.campaign_offer import CampaignOffer
from app.models.campaign_run import CampaignRun
from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignAudienceFilter, CampaignStatus, CampaignType,
    ClientCommunicationPreference, ConsentStatus, MessageChannel,
    MessageDeliveryStatus, MessagePurpose, MessageRecipient,
)
from app.models.segment import CustomerSegment
from app.models.sms_queue import SmsQueueJob
from app.schemas.campaign_run import CampaignAudiencePreviewResponse
from app.services import campaign_runs
from app.services.messaging import MessagingService
from app.services.segments import segment_service
from app.services.sms import SmsService
from tests.test_segments_integration import add_booking, anyio_backend, database  # noqa: F401


pytestmark = pytest.mark.anyio
AT = datetime(2026, 10, 9, 12, tzinfo=ZoneInfo("Europe/Kyiv"))
IMPORTED = AT.astimezone(UTC) - timedelta(days=180)
RULES = [
    {"type": "last_visit_age", "min": 3, "max": 12, "unit": "calendar_months",
     "min_inclusive": True, "max_inclusive": True},
    {"type": "last_visit_age", "min": 90, "max": 240, "unit": "days",
     "min_inclusive": True, "max_inclusive": True},
]
REASONS = {
    2: "Client is marked do-not-contact", 3: "Client has no marketing consent",
    6: "upcoming_booking", 7: "marketing_frequency_cap",
    8: "channel_unreachable", 13: "Client is marked do-not-contact",
    503: "Client opted out of marketing messages", 506: "upcoming_booking",
    507: "marketing_frequency_cap", 513: "unsupported_sms_characters",
}


def customer_fields(number):
    name, surname = f"Client {number:04}", "Family"
    if number in {13, 513}:
        name = f"Client {number} 😀"
    elif number == 514:
        name, surname = "L" * 100, "S" * 90
    return dict(id=number, name=name, surname=surname,
                phone="" if number == 8 else f"+38050{number:07}")


async def seed(database, *, legacy=False, moving_boundary=False):
    async with database() as session:
        customers = [Customer(**customer_fields(number), imported_last_visit_at=IMPORTED,
                              is_active=number != 4) for number in range(1, 1002)]
        # Five live customers outside both segments must never enter totals.
        customers += [Customer(**customer_fields(number), imported_last_visit_at=AT - timedelta(days=1))
                      for number in range(1002, 1007)]
        segments = [CustomerSegment(id=index, name=f"Synthetic overlapping segment {index}",
                                   rules={"combine": "all", "conditions": [rule], "exclusions": []})
                    for index, rule in enumerate(RULES, 1)]
        campaign = Campaign(
            name="Synthetic paged preview", type=CampaignType.manual, status=CampaignStatus.draft,
            purpose=MessagePurpose.marketing, channel=MessageChannel.telegram,
            metadata_json={"message_body": "Hello {{client_name}}!", "segment_ids": [] if legacy else [1, 2],
                           "channel_strategy": "telegram_then_sms", "exclude_upcoming_booking": True,
                           "marketing_frequency_days": 7},
        )
        previous = Campaign(name="Synthetic previous contact", type=CampaignType.manual,
                            status=CampaignStatus.active, purpose=MessagePurpose.marketing,
                            channel=MessageChannel.sms)
        session.add_all([*customers, *segments, campaign, previous])
        await session.flush()
        if legacy:
            session.add(CampaignAudienceFilter(campaign_id=campaign.id,
                                              criteria={"all_clients": True, "limit": 750}))
        preference_overrides = dict([
            (2, {"do_not_contact": True}), (13, {"do_not_contact": True}),
            (3, {"marketing_consent": ConsentStatus.unknown}),
            (503, {"marketing_consent": ConsentStatus.opted_out}),
            (5, {"telegram_chat_id": "synthetic-5"}),
            (505, {"telegram_chat_id": "synthetic-505"}),
        ])
        # Marketing eligibility uses recorded opt-in; missing preferences are not consent.
        # Preserve the dedicated unknown, opt-out, DNC and reachability controls.
        for customer in customers:
            data = {"marketing_consent": ConsentStatus.opted_in,
                    **preference_overrides.get(customer.id, {})}
            session.add(ClientCommunicationPreference(customer_id=customer.id, **data))
        for number in (7, 507):
            session.add(MessageRecipient(
                campaign_id=previous.id, customer_id=number, channel=MessageChannel.sms,
                status=MessageDeliveryStatus.sent, sent_at=AT - timedelta(days=1),
                idempotency_key=f"synthetic-preview-contact-{number}",
            ))
        for number in (6, 506):
            await add_booking(session, customers[number - 1], AT + timedelta(days=1), BookingStatus.confirmed)
        if moving_boundary:
            # add_booking receives end_at; place start_at exactly one second after AT.
            await add_booking(session, customers[0], AT + timedelta(minutes=30, seconds=1), BookingStatus.confirmed)
        await session.commit()
        return campaign.id


def expected_facts(number, *, moving_boundary=False):
    fields = customer_fields(number)
    upcoming = number in {6, 506} or (moving_boundary and number == 1)
    output = {}
    for index, rule in enumerate(RULES, 1):
        if index == 1:
            minimum = datetime(2026, 7, 9, 9, tzinfo=UTC)
            maximum = datetime(2025, 10, 9, 9, tzinfo=UTC)
        else:
            minimum = AT.astimezone(UTC) - timedelta(days=90)
            maximum = AT.astimezone(UTC) - timedelta(days=240)
        output[str(index)] = {
            "customer_id": number, "name": fields["name"], "phone": fields["phone"],
            "history_state": "known", "last_visit_at": IMPORTED,
            "completed_visit_count": 0, "first_completed_visit_at": None,
            "has_upcoming_booking": upcoming,
            "conditions": [{"rule": rule, "matched": True, "value": IMPORTED.isoformat(),
                            "min_cutoff": minimum.isoformat(), "max_cutoff": maximum.isoformat()}],
            "exclusions": [],
        }
    return output


def expected_member(number, *, legacy=False, moving_boundary=False):
    fields = customer_fields(number)
    reason = "upcoming_booking" if moving_boundary and number == 1 else REASONS.get(number)
    channel = "telegram" if number in {5, 505} else None if number == 8 else "sms"
    return {
        "customer_id": number, "name": fields["name"], "eligible": reason is None,
        "exclusion_reason": reason, "channel": channel,
        "provider_supported": number not in {13, 513},
        "rendered_message": f"Hello {fields['name']} {fields['surname']}!",
        "sms_parts": (2 if number == 514 else 1) if channel == "sms" else None,
        "reachability": {"sms": bool(fields["phone"]), "telegram": number in {5, 505}},
        "facts": {} if legacy else expected_facts(number, moving_boundary=moving_boundary),
    }


def expected_response(page, *, legacy=False, moving_boundary=False):
    # The inactive flag imposes no exclusion; the legacy global limit precedes paging.
    audience = list(range(1, 1007))[:750] if legacy else list(range(1, 1002))
    selected = audience[(page - 1) * 50:page * 50]
    return CampaignAudiencePreviewResponse.model_validate({
        "evaluated_at": AT, "total": 750 if legacy else 1001, "page": page, "page_size": 50,
        "items": [expected_member(number, legacy=legacy, moving_boundary=moving_boundary) for number in selected],
        "personalization_invalid_recipients": 2,
        "sms_eligible_recipients": (738 if legacy else 989) - int(moving_boundary),
        "telegram_eligible_recipients": 2,
        "communication_eligible_recipients": (740 if legacy else 991) - int(moving_boundary),
        "estimated_sms_parts": (739 if legacy else 990) - int(moving_boundary),
        "estimated_cost": {"status": "unavailable", "amount": None},
        "balance": {"status": "unsupported", "amount": None},
    }).model_dump(mode="json"), audience


class PreviewWork:
    def __init__(self):
        self.sql_count = 0
        self.dml = []
        self.commits = 0
        self.fact_batches = []
        self.rendered = Counter()
        self.segment_loads = 0
        self.audience_times = []

    @contextmanager
    def trace(self, database, monkeypatch, service):
        engine = database.kw["bind"].sync_engine

        def before_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            sql = statement.lstrip().upper()
            self.sql_count += 1
            if sql.startswith(("INSERT ", "UPDATE ", "DELETE ", "MERGE ", "CREATE ", "ALTER ", "DROP ")):
                self.dml.append(statement)

        def commit(_connection):
            self.commits += 1

        real_facts = segment_service.member_facts
        real_render = service.messaging.render_for_customer
        real_segments = service._load_segments
        real_audience = service._audience_statement

        async def facts(session, ids, rules, evaluated_at):
            assert len(ids) <= 500
            self.fact_batches.append({"rows": len(ids), "evaluated_at": evaluated_at.isoformat()})
            return await real_facts(session, ids, rules, evaluated_at)

        async def render(session, body, customer, *args, **kwargs):
            self.rendered[customer.id] += 1
            return await real_render(session, body, customer, *args, **kwargs)

        async def segments(*args, **kwargs):
            self.segment_loads += 1
            return await real_segments(*args, **kwargs)

        def audience(campaign, snapshots, evaluated_at):
            self.audience_times.append(evaluated_at.isoformat())
            return real_audience(campaign, snapshots, evaluated_at)

        monkeypatch.setattr(segment_service, "member_facts", facts)
        monkeypatch.setattr(service.messaging, "render_for_customer", render)
        monkeypatch.setattr(service, "_load_segments", segments)
        monkeypatch.setattr(service, "_audience_statement", audience)
        event.listen(engine, "before_cursor_execute", before_sql)
        event.listen(engine, "commit", commit)
        try:
            yield
        finally:
            event.remove(engine, "before_cursor_execute", before_sql)
            event.remove(engine, "commit", commit)

    def record(self, record_property):
        record_property("campaign_preview_work", json.dumps({
            "sql_statements": self.sql_count, "segment_loads": self.segment_loads,
            "audience_evaluation_times": self.audience_times, "fact_batches": self.fact_batches,
            "fact_rows": sum(batch["rows"] for batch in self.fact_batches),
            "render_calls": sum(self.rendered.values()), "distinct_rendered_customers": len(self.rendered),
            "dml": self.dml, "commits": self.commits,
        }, sort_keys=True))


async def persisted_counts(session):
    models = (Customer, CustomerSegment, Campaign, ClientCommunicationPreference,
              Booking, CampaignRun, MessageRecipient, CampaignOffer, SmsQueueJob)
    return {model.__tablename__: await session.scalar(select(func.count()).select_from(model)) for model in models}


async def exercise(database, monkeypatch, page, *, legacy=False, moving_boundary=False, record_property):
    campaign_id = await seed(database, legacy=legacy, moving_boundary=moving_boundary)
    clock_calls = []

    class PreviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = AT + timedelta(seconds=2 * len(clock_calls)) if moving_boundary else AT
            clock_calls.append(value)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(campaign_runs, "datetime", PreviewClock)
    provider_calls = []

    class ForbiddenProvider:
        async def send_message(self, **kwargs):
            provider_calls.append(kwargs)
            raise AssertionError("Read-only preview must not invoke a provider")

    def forbidden_sms_transport(*args, **kwargs):
        provider_calls.append("direct_sms_transport")
        raise AssertionError("Read-only preview must not invoke SMS transport")

    monkeypatch.setattr(SmsService, "_post_json", forbidden_sms_transport)
    messaging = MessagingService(providers={channel: ForbiddenProvider() for channel in MessageChannel})
    service = campaign_runs.CampaignRunService(messaging)
    work = PreviewWork()
    expected, audience = expected_response(page, legacy=legacy, moving_boundary=moving_boundary)
    async with database() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        campaign = await messaging.get_campaign(session, campaign_id)
        before = await persisted_counts(session)
        with work.trace(database, monkeypatch, service):
            result = await service.preview(session, campaign, page=page, page_size=50)
        actual = CampaignAudiencePreviewResponse.model_validate(result).model_dump(mode="json")
        assert await persisted_counts(session) == before
        assert not session.new and not session.dirty and not session.deleted
        assert before["campaign_runs"] == before["campaign_offers"] == before["sms_queue_jobs"] == 0
        assert before["message_recipients"] == 2
        assert work.sql_count > 0 and work.dml == [] and work.commits == 0
        assert provider_calls == []
        assert set(work.rendered) == set(audience)
        work.record(record_property)
        record_property("preview_clock_calls", json.dumps([value.isoformat() for value in clock_calls]))
        aggregate_keys = ("total", "personalization_invalid_recipients", "sms_eligible_recipients",
                          "telegram_eligible_recipients", "communication_eligible_recipients", "estimated_sms_parts")
        record_property("preview_aggregate_golden", json.dumps({
            "actual": {key: actual[key] for key in aggregate_keys},
            "expected": {key: expected[key] for key in aggregate_keys},
        }, sort_keys=True))
        assert actual == expected
        if moving_boundary:
            assert set(work.audience_times) == {AT.isoformat()}
            assert {batch["evaluated_at"] for batch in work.fact_batches} == {AT.isoformat()}


@pytest.mark.parametrize("page", [1, 11, 21, 22], ids=["first", "middle", "final", "empty"])
async def test_overlapping_segment_preview_pages_preserve_wire_goldens_and_read_only_work(
    database, monkeypatch, page, record_property,
):
    await exercise(database, monkeypatch, page, record_property=record_property)


@pytest.mark.parametrize("page", [15, 16], ids=["legacy-final", "legacy-empty"])
async def test_legacy_global_limit_is_applied_before_preview_pagination(
    database, monkeypatch, page, record_property,
):
    await exercise(database, monkeypatch, page, legacy=True, record_property=record_property)


async def test_preview_totals_honor_selected_page_evaluation_instant_when_clock_crosses_booking_start(
    database, monkeypatch, record_property,
):
    await exercise(database, monkeypatch, 1, moving_boundary=True, record_property=record_property)
