"""Archive behavior against an isolated SQLite database and actual ORM queries."""
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import aliased, selectinload

import app.models  # Register all relations for isolated metadata creation.
from app.api.v1.routes import bookings as routes
from app.core.database import Base, OperationalSession, get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_master
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.booking_recovery import BookingRecoveryEvent, BookingRecoveryEventType
from app.models.campaign_offer import CampaignOffer
from app.models.customer import Customer
from app.models.messaging import MessageChannel, MessageDeliveryStatus, MessageRecipient, ReviewPlatform, ReviewRequest, ReviewRequestStatus
from app.models.repeat_booking import RepeatBookingEvent, RepeatBookingEventType, RepeatBookingOffer, RepeatBookingOfferStatus
from app.services.booking import BookingServiceLayer
from app.services.booking_archive import archive_booking
from app.services.booking_recovery_analytics import BookingRecoveryAnalyticsService
from app.services.campaign_analytics import offer_analytics
from app.services.promotion import PromotionService
from app.services.repeat_booking import RepeatBookingService

AT = datetime(2026, 10, 11, 12, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class AsyncAdapter:
    def __init__(self, session):
        self.session = session
        self.no_autoflush = session.no_autoflush

    async def execute(self, statement):
        return self.session.execute(statement)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def get(self, model, identifier, **kwargs):
        return self.session.get(model, identifier, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    def add(self, item):
        self.session.add(item)

    @asynccontextmanager
    async def begin_nested(self):
        with self.session.begin_nested():
            yield


def add_booking(session, identifier=1, **changes):
    values = dict(id=identifier, master_id=1, service_id=1, customer_id=1,
                  customer_name="Archive Customer", customer_phone="+380500000001",
                  start_at=AT, end_at=AT + timedelta(hours=1),
                  status=BookingStatus.completed, completed_at=AT + timedelta(hours=8),
                  subtotal_amount=1000, total_amount=800, promotion_discount_amount=200)
    values.update(changes)
    item = Booking(**values)
    session.add(item)
    session.flush()
    return item


@pytest.fixture
def database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with OperationalSession(engine, expire_on_commit=False) as session:
        session.add_all([
            Master(id=1, full_name="Archive Master", is_active=True),
            Customer(id=1, name="Archive Customer", phone="+380500000001",
                     first_visit_completed_at=AT + timedelta(hours=1)),
            BarberService(id=1, master_id=1, name="Haircut", duration_minutes=60, price=1000, is_active=True),
        ])
        session.flush()
        booking = add_booking(session)
        session.add(BookingServiceItem(booking_id=1, service_id=1, position=0, price_amount=1000))
        session.commit()
        yield session, AsyncAdapter(session), booking
    engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("path,superuser,owner,expected", [
    ("/masters/me/bookings/1", False, 1, 204),
    ("/masters/me/bookings/1", False, 2, 403),
    ("/bookings/1", True, 2, 204),
    ("/bookings/1", False, 1, 204),
    ("/bookings/1", False, 2, 403),
])
async def test_delete_archives_completed_visits_and_enforces_ownership(database, path, superuser, owner, expected):
    session, adapter, booking = database
    owner_master = session.get(Master, 1) if owner == 1 else Master(id=2, admin_user_id=102, full_name="Other")
    owner_master.admin_user_id = 101 if owner == 1 else 102
    if owner == 2:
        session.add(owner_master)
    session.commit()
    app = FastAPI()
    app.include_router(routes.backoffice_router)
    async def dependency():
        yield adapter
    app.dependency_overrides[get_db_session] = dependency
    app.dependency_overrides[get_current_master] = lambda: owner_master
    app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=owner_master.admin_user_id, is_superuser=superuser)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(path)
        assert response.status_code == expected
        if expected == 204:
            archived_at = booking.archived_at
            assert archived_at is not None
            assert (await client.delete(path)).status_code == 204
            assert booking.archived_at == archived_at
            assert (await client.get(path)).status_code == 404
    assert booking.status == BookingStatus.completed
    assert booking.completed_at is not None
    assert session.scalar(select(func.count()).select_from(Booking).execution_options(include_archived_bookings=True)) == 1
    assert session.scalar(select(func.count()).select_from(BookingServiceItem)) == 1
    if expected == 403:
        assert booking.archived_at is None


@pytest.mark.anyio
async def test_archived_bookings_disappear_from_orm_counts_aliases_subqueries_relationships_and_slots(database):
    session, adapter, booking = database
    active = add_booking(session, 2, status=BookingStatus.confirmed, completed_at=None)
    await archive_booking(adapter, booking)
    await adapter.commit()
    assert session.scalars(select(Booking.id)).all() == [active.id]
    assert session.scalar(select(func.count()).select_from(Booking)) == 1
    assert session.scalar(select(func.sum(Booking.total_amount))) == 800
    calendar = aliased(Booking)
    assert session.scalars(select(calendar.id)).all() == [active.id]
    assert session.scalar(select(func.sum(calendar.total_amount))) == 800
    visits = select(Booking.customer_id, func.count(Booking.id).label("total")).group_by(Booking.customer_id).subquery()
    assert session.execute(select(visits.c.total)).scalar_one() == 1
    assert session.execute(select(Customer.id, func.count(Booking.id)).outerjoin(Booking, Booking.customer_id == Customer.id).group_by(Customer.id)).one() == (1, 1)
    customer = session.scalars(select(Customer).options(selectinload(Customer.bookings)).execution_options(populate_existing=True)).one()
    assert [visit.id for visit in customer.bookings] == [2]
    session.expunge(booking)
    assert session.get(Booking, 1) is None
    assert session.scalars(select(Booking.id).order_by(Booking.id).execution_options(include_archived_bookings=True)).all() == [1, 2]
    assert [item.id for item in await BookingServiceLayer().list_busy_bookings(adapter, 1, AT, AT + timedelta(hours=2))] == [2]


@pytest.mark.anyio
@pytest.mark.parametrize("remaining,imported", [(False, False), (True, False), (False, True)])
async def test_archive_recomputes_first_visit_entitlement_without_erasing_imported_history(database, remaining, imported):
    session, adapter, booking = database
    customer = session.get(Customer, 1)
    booking.first_visit_customer_id = 1
    if remaining:
        add_booking(session, 2, end_at=AT + timedelta(hours=2))
    if imported:
        customer.imported_last_visit_at = AT - timedelta(days=200)
        customer.imported_total_spent = 1234
    session.commit()
    await archive_booking(adapter, booking)
    await adapter.commit()
    assert booking.first_visit_customer_id is None
    assert customer.first_visit_completed_at == (datetime(2026, 10, 11, 14) if remaining else None)
    assert await PromotionService().first_visit_reason(adapter, customer) == ("not_eligible" if remaining or imported else "eligible")
    if imported:
        assert customer.imported_total_spent == 1234
        assert customer.imported_last_visit_at is not None


@pytest.mark.anyio
async def test_archive_revokes_review_repeat_and_pending_delivery_but_keeps_campaign_audit(database):
    session, adapter, booking = database
    booking.campaign_offer_id = 9
    booking.campaign_offer_reservation_id = 9
    offer = CampaignOffer(id=9, campaign_id=1, run_id=1, recipient_id=99, customer_id=1,
                          master_id=1, promotion_id=1, token_hash="campaign-audit", allowed_service_ids=[1],
                          starts_at=AT, expires_at=AT + timedelta(days=1), promotion_name_uk="Знижка",
                          promotion_name_en="Discount", consumed_booking_id=1, completed_at=AT,
                          redeemed_discount_amount=200, redeemed_revenue_amount=800)
    repeat = RepeatBookingOffer(id=1, completed_booking_id=1, customer_id=1, service_ids=[1],
                               status=RepeatBookingOfferStatus.sent, scheduled_at=AT, token_hash="repeat-token")
    review = ReviewRequest(id=1, campaign_id=1, appointment_id=1, customer_id=1, master_id=1,
                           platform=ReviewPlatform.internal, review_url="/review", status=ReviewRequestStatus.sent,
                           token_hash="review-token", expires_at=AT + timedelta(days=1))
    recipient = MessageRecipient(id=1, campaign_id=1, appointment_id=1, customer_id=1,
                                 channel=MessageChannel.sms, status=MessageDeliveryStatus.pending,
                                 scheduled_at=AT, idempotency_key="archive-message", rendered_message="Visit")
    session.add_all([offer, repeat, review, recipient])
    session.commit()
    before = await offer_analytics(adapter)
    assert (before["completed_visits"], before["observed_completed_revenue"]) == (1, 800)
    await archive_booking(adapter, booking)
    await adapter.commit()
    assert (booking.campaign_offer_id, booking.campaign_offer_reservation_id) == (9, None)
    assert (offer.consumed_booking_id, offer.redeemed_revenue_amount) == (1, 800)
    assert repeat.status == RepeatBookingOfferStatus.skipped
    assert repeat.token_hash is None and repeat.revoked_at is not None
    assert review.token_hash is None and review.status == ReviewRequestStatus.failed
    assert recipient.status == MessageDeliveryStatus.skipped
    after = await offer_analytics(adapter)
    assert (after["bookings_created"], after["completed_visits"], after["unique_customers_redeemed"],
            after["total_discount_amount"], after["observed_completed_revenue"]) == (0, 0, 0, 0, 0)


@pytest.mark.anyio
async def test_repeat_completion_analytics_excludes_archived_result(database):
    session, adapter, result = database
    source = add_booking(session, 2)
    offer = RepeatBookingOffer(id=1, completed_booking_id=2, result_booking_id=1, customer_id=1,
                               service_ids=[1], status=RepeatBookingOfferStatus.booked, scheduled_at=AT)
    session.add(offer)
    session.add(RepeatBookingEvent(offer_id=1, event_key_hash="repeat-completed", event_type=RepeatBookingEventType.booking_completed.value, created_at=AT))
    session.commit()
    service = RepeatBookingService()
    assert (await service.analytics(adapter, date_from=date(2026, 10, 11), date_to=date(2026, 10, 11))).completed_repeat_visits == 1
    await archive_booking(adapter, result)
    await adapter.commit()
    assert (await service.analytics(adapter, date_from=date(2026, 10, 11), date_to=date(2026, 10, 11))).completed_repeat_visits == 0
    assert session.scalar(select(func.count()).select_from(RepeatBookingEvent)) == 1


@pytest.mark.anyio
async def test_recovery_completion_analytics_excludes_archived_bookings(database):
    session, adapter, booking = database
    session.add_all([
        BookingRecoveryEvent(event_key_hash="completion", event_type=BookingRecoveryEventType.booking_completed_after_alternative.value,
                             booking_id=1, occurred_at=AT, anonymous_session_hash="session", search_context_hash="context"),
        BookingRecoveryEvent(event_key_hash="refill", event_type=BookingRecoveryEventType.booking_completed_after_waitlist_offer.value,
                             booking_id=1, occurred_at=AT, metric_value=15),
    ])
    session.commit()
    service = BookingRecoveryAnalyticsService()
    before = await service.summary(adapter, date_from=date(2026, 10, 11), date_to=date(2026, 10, 11))
    assert before.bookings_after_alternative == 1
    await archive_booking(adapter, booking)
    await adapter.commit()
    after = await service.summary(adapter, date_from=date(2026, 10, 11), date_to=date(2026, 10, 11))
    assert after.bookings_after_alternative == 0
    assert after.cancelled_slots_refilled == 0
    assert session.scalar(select(func.count()).select_from(BookingRecoveryEvent)) == 2



def test_archive_migration_preserves_existing_rows_and_can_downgrade():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).parents[1] / "alembic/versions/0108_booking_archive.py"
    spec = importlib.util.spec_from_file_location("booking_archive_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0107_mobile_inventory"
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE bookings (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO bookings(id) VALUES (1)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(text("SELECT id, archived_at FROM bookings")).one() == (1, None)
        assert any(index["name"] == "ix_bookings_archived_at" for index in inspect(connection).get_indexes("bookings"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.execute(text("SELECT id FROM bookings")).scalar_one() == 1
        assert "archived_at" not in {column["name"] for column in inspect(connection).get_columns("bookings")}
    engine.dispose()


@pytest.mark.anyio
async def test_telegram_owned_session_uses_operational_archive_filter(database, monkeypatch):
    from unittest.mock import Mock
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import Session
    from app.services import telegram_inbox

    session, _, booking = database
    booking.archived_at = AT
    session.commit()
    connection = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    @asynccontextmanager
    async def connect():
        yield connection
    engine = Mock(spec=AsyncEngine)
    engine.connect = connect

    class OwnedAdapter(AsyncAdapter):
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            self.session.close()

    def create_owned(*, bind, sync_session_class=Session, expire_on_commit=False):
        assert bind is connection
        return OwnedAdapter(sync_session_class(session.bind, expire_on_commit=expire_on_commit))

    monkeypatch.setattr(telegram_inbox, "AsyncSession", create_owned)
    async def action(owned):
        assert await owned.scalar(select(func.count()).select_from(Booking)) == 0
        return {"handled": True, "action": "archive_filter_verified"}
    result = await telegram_inbox.process_telegram_event(SimpleNamespace(bind=engine),
        update_id=12345, chat_id="synthetic-chat", action=action)
    assert result["handled"] is True
