from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.booking import Booking, BookingStatus
from app.models.promotion import PromotionApplicationMode, PromotionDiscountType, PromotionEligibilityType
from app.services.promotion import PromotionService
from app.schemas.promotion import PromotionCreate

AT = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def offer(**overrides):
    values = dict(id=1, code="FIRST_VISIT", name_uk="Перший візит", name_en="First visit",
                  application_mode=PromotionApplicationMode.automatic,
                  eligibility_type=PromotionEligibilityType.first_visit,
                  discount_type=PromotionDiscountType.percent, discount_percent=20,
                  applies_to_all_masters=True, applies_to_all_services=True,
                  master_ids=[], base_service_ids=[], is_active=True, is_public=True,
                  starts_at=None, ends_at=None)
    values.update(overrides)
    return SimpleNamespace(**values)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value if isinstance(self.value, list) else [self.value]


class Session:
    no_autoflush = nullcontext()

    def __init__(self, customer, history=None, reservation=None, promotion=None):
        self.customer = customer
        self.history = history
        self.reservation = reservation
        self.promotion = promotion
        self.statements = []
        self.alias_imported = None

    async def execute(self, statement):
        self.statements.append(str(statement))
        if "FROM bookings" not in str(statement) and "FROM customers" in str(statement):
            return Result(self.alias_imported if "SELECT customers.id \n" in str(statement) else self.customer)
        if "FROM promotions" in str(statement):
            return Result(self.promotion)
        if "bookings.first_visit_customer_id" in str(statement):
            return Result(self.reservation)
        return Result(self.history)

    async def get(self, model, key):
        return self.promotion if model.__name__ == "Promotion" else self.customer


class Service(PromotionService):
    def __init__(self, offers):
        self.offers = offers

    async def list_active_public_catalog_promotions(self, session, *, at):
        return [p for p in self.offers if p.is_active and (not p.starts_at or p.starts_at <= at)
                and (not p.ends_at or p.ends_at >= at)]


def customer(**kwargs):
    return SimpleNamespace(id=1, phone="+380501112233", imported_last_visit_at=None,
                           imported_total_spent=0, first_visit_completed_at=None, **kwargs)


def booking():
    return Booking(customer_id=1, customer_phone="+380501112233", start_at=AT,
                   end_at=AT + timedelta(hours=1), status=BookingStatus.confirmed)


def services(master=1):
    return [SimpleNamespace(id=1, master_id=master, base_service_id=10, price=1000)]


@pytest.mark.anyio
@pytest.mark.parametrize("master", [1, 2, 99])
async def test_automatic_for_new_customer_across_masters(master):
    c, b = customer(), booking()
    await Service([offer()]).apply_to_booking(Session(c), booking=b, promotion_code=None,
                                            customer=c, services=services(master), at=AT)
    assert (b.subtotal_amount, b.promotion_discount_amount, b.total_amount) == (1000, 200, 800)
    assert b.first_visit_customer_id == c.id
    assert b.promotion_code_snapshot is None


@pytest.mark.anyio
@pytest.mark.parametrize("evidence", ["imported_last_visit_at", "imported_total_spent", "first_visit_completed_at", "history", "reservation"])
async def test_returning_or_reserved_customer_not_automatically_discounted(evidence):
    c, b = customer(), booking()
    session = Session(c)
    if evidence in ("history", "reservation"):
        setattr(session, evidence, 42)
    else:
        setattr(c, evidence, 100 if evidence == "imported_total_spent" else AT)
    await Service([offer()]).apply_to_booking(session, booking=b, promotion_code=None,
                                            customer=c, services=services(), at=AT)
    assert b.total_amount == 1000
    assert b.first_visit_customer_id is None


@pytest.mark.anyio
async def test_live_percentage_changes_only_new_bookings():
    c, old, new, p = customer(), booking(), booking(), offer()
    service = Service([p])
    session = Session(c, promotion=p)
    await service.apply_to_booking(session, booking=old, promotion_code=None, customer=c, services=services(), at=AT)
    p.discount_percent = 35
    p.is_active = False
    await service.apply_to_booking(session, booking=old, promotion_code=None, customer=c, services=services(), at=AT, preserve_existing=True)
    assert old.total_amount == 800
    p.is_active = True
    await service.apply_to_booking(session, booking=new, promotion_code=None, customer=c, services=services(), at=AT)
    assert new.total_amount == 650


@pytest.mark.anyio
@pytest.mark.parametrize("changes", [dict(is_active=False), dict(starts_at=AT + timedelta(days=1)),
                                    dict(ends_at=AT-timedelta(days=1)),
                                    dict(applies_to_all_masters=False, master_ids=[2]),
                                    dict(applies_to_all_services=False, base_service_ids=[20])])
async def test_automatic_scope_activation_and_dates(changes):
    c, b = customer(), booking()
    await Service([offer(**changes)]).apply_to_booking(Session(c), booking=b, promotion_code=None,
                                                     customer=c, services=services(), at=AT)
    assert b.total_amount == 1000


@pytest.mark.anyio
async def test_explicit_code_wins_and_automatic_identifier_cannot_be_entered():
    c, b = customer(), booking()
    p = offer(application_mode=PromotionApplicationMode.code, eligibility_type=PromotionEligibilityType.all_customers,
              code="CODE10", discount_percent=10)
    service = Service([offer()])
    await service.apply_to_booking(Session(c, promotion=p), booking=b, promotion_code="CODE10",
                                   customer=c, services=services(), at=AT)
    assert b.total_amount == 900
    assert b.promotion_code_snapshot == "CODE10"
    assert b.first_visit_customer_id is None
    with pytest.raises(HTTPException):
        await service.get_active_by_code(Session(c, promotion=offer()), "FIRST_VISIT", at=AT)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [BookingStatus.cancelled, BookingStatus.no_show])
async def test_cancellation_and_no_show_release_without_consuming(status):
    c, b = customer(), booking()
    b.first_visit_customer_id = c.id
    await Service([]).sync_first_visit_status(Session(c), b, status)
    assert b.first_visit_customer_id is None
    assert c.first_visit_completed_at is None


@pytest.mark.anyio
async def test_completion_of_regular_booking_durably_consumes_entitlement():
    c, b = customer(), booking()
    await Service([]).sync_first_visit_status(Session(c), b, BookingStatus.completed)
    assert c.first_visit_completed_at == b.end_at


@pytest.mark.anyio
async def test_preview_does_not_reserve_or_lock():
    c, b = customer(), booking()
    session = Session(c)
    await Service([offer()]).apply_to_booking(session, booking=b, promotion_code=None,
                                            customer=c, services=services(), at=AT, reserve_entitlement=False)
    assert b.total_amount == 800
    assert b.first_visit_customer_id is None
    assert all("FOR UPDATE" not in stmt for stmt in session.statements)


@pytest.mark.anyio
async def test_snapshot_reschedule_rejects_new_ineligible_date_without_repricing():
    c, b, p = customer(), booking(), offer(ends_at=AT + timedelta(days=1))
    service, session = Service([p]), Session(c, promotion=p)
    await service.apply_to_booking(session, booking=b, promotion_code=None, customer=c, services=services(), at=AT)
    with pytest.raises(HTTPException) as exc:
        await service.apply_to_booking(session, booking=b, promotion_code=None, customer=c, services=services(),
                                       at=AT + timedelta(days=2), preserve_existing=True)
    assert exc.value.detail["code"] == "promotion_no_longer_applicable"
    assert b.total_amount == 800


def test_automatic_military_requires_verification_at_create_and_patch():
    with pytest.raises(ValueError, match="Military promotions require"):
        PromotionCreate(code="ARMY", name_uk="Захисникам", name_en="Military", discount_percent=50,
                        application_mode="automatic", eligibility_type="military_customers")
    service = PromotionService()
    with pytest.raises(HTTPException, match="Military promotions require"):
        service.complete_update_data(offer(eligibility_type=PromotionEligibilityType.military_customers,
                                           application_mode=PromotionApplicationMode.code),
                                     {"application_mode": PromotionApplicationMode.automatic})
    with pytest.raises(HTTPException, match="Military promotions require"):
        service.complete_update_data(offer(), {"eligibility_type": PromotionEligibilityType.military_customers})


@pytest.mark.anyio
async def test_telegram_cancellation_locks_booking_before_customer_entitlement():
    from app.api.v1.routes.messaging import _telegram_cancellable_booking

    session = Session(customer())
    await _telegram_cancellable_booking(session, booking_id=1, customer_id=1, session_booking_id=None)
    assert "FOR UPDATE" in session.statements[0]
    assert "LIMIT" not in session.statements[0]


@pytest.mark.parametrize("raw,canonical,aliases", [
    ("+0501234567", "+380501234567", {"+0501234567", "+380501234567"}),
    ("+380501234567", "+380501234567", {"+0501234567", "+380501234567"}),
    ("+441234567890", "+441234567890", {"+441234567890"}),
    ("+0123", "+0123", {"+0123"}),
])
def test_booking_phone_aliases(raw, canonical, aliases):
    from app.utils.booking_identity import canonical_booking_phone, phone_aliases
    assert canonical_booking_phone(raw) == canonical
    assert phone_aliases(raw) == aliases


@pytest.mark.anyio
async def test_imported_evidence_on_duplicate_phone_alias_disqualifies():
    c = customer()
    session = Session(c)
    session.alias_imported = 99
    assert await PromotionService().first_visit_reason(session, c) == "not_eligible"
    assert "customers.phone IN" in session.statements[0]


@pytest.mark.anyio
@pytest.mark.parametrize("evidence,reason", [("history", "not_eligible"), ("reservation", "entitlement_reserved")])
async def test_booking_evidence_queries_include_duplicate_customer_aliases(evidence, reason):
    c = customer()
    session = Session(c)
    setattr(session, evidence, 99)
    assert await PromotionService().first_visit_reason(session, c) == reason
    assert "customers.phone IN" in session.statements[-1]
    assert "SELECT customers.id" in session.statements[-1]


@pytest.mark.anyio
async def test_customer_lock_includes_all_phone_aliases_in_id_order():
    c = customer()
    session = Session(c)
    assert await PromotionService().lock_customer(session, c) is c
    assert "customers.phone IN" in session.statements[0]
    assert "ORDER BY customers.id" in session.statements[0]
    assert "FOR UPDATE" in session.statements[0]
    assert "LIMIT" not in session.statements[0]
