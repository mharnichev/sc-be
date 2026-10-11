"""Archived appointments cannot dispatch snapshots or affect review statistics."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import sqlite3

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import sqlite

from app.models.booking import Booking, Master
from app.models.master_review import MasterReview
from app.models.messaging import Campaign, CampaignStatus, MessageDeliveryStatus, MessageRecipient
from app.services.master_reviews import MasterReviewService
from app.services.booking_sms_notifications import BookingSmsNotificationService
from app.services.sms_queue import SmsQueueService
from app.services.campaign_dispatch import CampaignDispatchService
from app.services.customer_activity_notifications import CustomerActivityNotificationService
from app.services.messaging import MessagingService

@pytest.fixture
def anyio_backend():
    return "asyncio"

class Result:
    def __init__(self, value):
        self.value = value
    def scalar_one_or_none(self):
        return self.value

class Session:
    def __init__(self, booking=None, recipient=None):
        self.booking = booking
        self.recipient = recipient
        self.added = []
        self.committed = False
    async def get(self, model, identifier, **kwargs):
        if model is Booking:
            return self.booking
        if model is MessageRecipient:
            return self.recipient
        if model is Campaign:
            return Campaign(status=CampaignStatus.active, metadata_json={})
        raise AssertionError("Unexpected model lookup")
    async def execute(self, statement):
        return Result(self.recipient)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def add(self, value):
        self.added.append(value)
    async def commit(self):
        self.committed = True

@pytest.mark.anyio
@pytest.mark.parametrize("booking", [None, SimpleNamespace(archived_at=datetime.now(UTC))])
async def test_booking_confirmation_rejects_removed_or_archived_snapshot(monkeypatch, booking):
    session = Session(booking)
    monkeypatch.setattr("app.services.booking_sms_notifications.AsyncSessionLocal", lambda: session)
    allowed, customer_id = await BookingSmsNotificationService().notification_communication_allowed(
        SimpleNamespace(booking_id=1, customer_phone="+380501112233"))
    assert (allowed, customer_id) == (False, None)

@pytest.mark.anyio
@pytest.mark.parametrize("booking", [None, SimpleNamespace(archived_at=datetime.now(UTC))])
async def test_queue_final_booking_gate_rejects_archived_snapshot(booking):
    job = SimpleNamespace(operation="send", context_json={"idempotency_key": "booking-confirmation:1:2099-01-01"})
    decision = await SmsQueueService()._booking_notification_gate(Session(booking), job)
    assert (decision.action, decision.reason) == ("skip", "booking_archived")

@pytest.mark.anyio
async def test_campaign_final_gate_rejects_linked_archived_booking():
    recipient = SimpleNamespace(appointment_id=1)
    session = Session(SimpleNamespace(archived_at=datetime.now(UTC)), recipient)
    job = SimpleNamespace(operation="send", priority=10, context_json={"campaign_id": 2, "recipient_id": 3})
    decision = await CampaignDispatchService().final_gate(session, job, datetime.now(UTC))
    assert (decision.action, decision.reason) == ("skip", "booking_archived")

@pytest.mark.anyio
async def test_activity_dispatch_skips_archived_before_minting_link_or_sending(monkeypatch):
    recipient = MessageRecipient(id=3, appointment_id=1, status=MessageDeliveryStatus.pending,
        idempotency_key="customer-activity:booking_confirmation:1", channel="sms", customer_id=2, campaign_id=4)
    recipient.appointment = None
    session = Session(recipient=recipient)
    monkeypatch.setattr("app.services.customer_activity_notifications.AsyncSessionLocal", lambda: session)
    assert await CustomerActivityNotificationService()._dispatch(3) is False
    assert recipient.status == MessageDeliveryStatus.skipped
    assert recipient.last_error == "booking_archived"
    assert session.committed

@pytest.mark.anyio
async def test_review_token_archived_relationship_is_unavailable():
    request = SimpleNamespace(expires_at=datetime.now(UTC)+timedelta(days=1), appointment=None)
    with pytest.raises(HTTPException) as exc:
        await MasterReviewService().get_request_by_token(Session(recipient=request), "valid-token")
    assert exc.value.status_code == 404

class SqlResult:
    def __init__(self, rows):
        self.rows = rows
    def one(self):
        return self.rows[0]
    def scalar_one(self):
        return self.rows[0][0]
    def all(self):
        return self.rows
    def scalars(self):
        return SimpleNamespace(all=lambda: [])

class SqlSession:
    def __init__(self, db):
        self.db = db
    async def get(self, model, identifier):
        assert model is Master
        return SimpleNamespace(id=identifier)
    async def execute(self, statement):
        sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        return SqlResult(self.db.execute(sql).fetchall())

@pytest.mark.anyio
async def test_archived_reviews_removed_from_counts_distribution_public_pages_and_aggregates():
    db = sqlite3.connect(":memory:")
    try:
        columns = ", ".join(f'"{c.name}"' for c in MasterReview.__table__.columns)
        db.execute(f"CREATE TABLE master_reviews ({columns})")
        db.execute("CREATE TABLE bookings (id INTEGER PRIMARY KEY, archived_at TEXT)")
        db.executemany("INSERT INTO bookings VALUES (?, ?)", [(1, None), (2, "2026-10-11")])
        db.executemany("INSERT INTO master_reviews (id, booking_id, master_id, rating, status) VALUES (?, ?, 1, ?, ?)",
            [(1, 1, 5, "approved"), (2, 2, 1, "approved"), (3, 2, 1, "pending")])
        session = SqlSession(db)
        summary = await MasterReviewService().rating_summary(session, 1)
        assert (summary.average_rating, summary.approved_review_count, summary.pending_review_count) == (5, 1, 0)
        assert summary.rating_distribution == {1: 0, 2: 0, 3: 0, 4: 0, 5: 1}
        aggregates = await MasterReviewService().approved_rating_aggregates(session, [1])
        assert (aggregates[1].average_rating, aggregates[1].review_count) == (5, 1)
        _, total = await MasterReviewService().public_reviews(session, 1, page=1, page_size=10)
        assert total == 1
    finally:
        db.close()

@pytest.mark.anyio
async def test_review_recipient_is_skipped_before_provider_call_when_booking_archived():
    from app.models.messaging import CampaignType, MessagePurpose
    recipient = MessageRecipient(id=3, appointment_id=1, status=MessageDeliveryStatus.pending,
        idempotency_key="review:1", channel="sms", customer_id=2, campaign_id=4)
    recipient.campaign = Campaign(purpose=MessagePurpose.review_request, type=CampaignType.post_visit_review_request)
    session = Session(booking=SimpleNamespace(archived_at=datetime.now(UTC)))
    await MessagingService().send_recipient(session, recipient)
    assert recipient.status == MessageDeliveryStatus.skipped
    assert recipient.last_error == "booking_archived"

@pytest.mark.anyio
async def test_legacy_bookingless_sms_policy_gate_remains_allowed():
    class LegacySession:
        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: [])
        async def get(self, *args, **kwargs):
            raise AssertionError("Legacy jobs must not load appointments")
    job = SimpleNamespace(operation="send", payload={"phone": []}, context_json={})
    decision = await SmsQueueService()._booking_notification_gate(LegacySession(), job)
    assert decision.action == "allow"

@pytest.mark.anyio
@pytest.mark.parametrize("booking", [None, SimpleNamespace(archived_at=datetime.now(UTC))])
async def test_archived_review_moderation_rejected_before_mutation_or_commit(booking):
    from app.models.master_review import MasterReviewStatus
    review = MasterReview(id=1, booking_id=2, status=MasterReviewStatus.pending,
        moderated_at=None, moderated_by=None, moderation_reason=None, published_at=None)
    class ModerationSession(Session):
        async def get(self, model, identifier, **kwargs):
            if model is MasterReview:
                return review
            return await super().get(model, identifier, **kwargs)
    session = ModerationSession(booking)
    with pytest.raises(HTTPException) as exc:
        await MasterReviewService().moderate(session, 1, new_status=MasterReviewStatus.approved,
            actor_id=3, reason="Approved")
    assert exc.value.status_code == 404
    assert review.status == MasterReviewStatus.pending
    assert review.moderated_at is None
    assert review.moderated_by is None
    assert review.moderation_reason is None
    assert review.published_at is None
    assert session.added == []
    assert session.committed is False
