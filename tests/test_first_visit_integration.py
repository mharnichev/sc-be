"""Real PostgreSQL first-visit checks using isolated opt-in schemas, never providers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from tests.test_segments_integration import database, anyio_backend  # noqa: F401
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.customer import Customer
from app.models.promotion import Promotion, PromotionApplicationMode, PromotionEligibilityType
from app.schemas.booking import PublicBookingCreate
from app.services.booking import BookingServiceLayer
from app.services.promotion import PromotionService

AT = datetime(2099, 1, 6, 12, tzinfo=ZoneInfo("Europe/Kyiv"))
PHONE = "+380501234567"


async def seed(database, *, customer=True, **history):
    async with database() as session:
        masters = [Master(full_name=f"Integration master {i}") for i in range(2)]
        session.add_all(masters)
        await session.flush()
        services = [BarberService(master_id=m.id, name="Haircut", duration_minutes=30, price=1000)
                    for m in masters]
        promotion = Promotion(code="FIRST_VISIT", name_uk="Перший візит", name_en="First visit",
                              discount_percent=20, eligibility_type=PromotionEligibilityType.first_visit,
                              application_mode=PromotionApplicationMode.automatic)
        session.add_all([*services, promotion])
        if customer:
            session.add(Customer(phone=PHONE, name="Integration customer", **history))
        await session.commit()
        return [(m.id, s.id) for m, s in zip(masters, services)], promotion.id


async def create(database, pair, *, phone=PHONE, at=AT, code=None):
    async with database() as session:
        return await BookingServiceLayer().create_public_booking(
            session, PublicBookingCreate(master_id=pair[0], service_id=pair[1],
                                         customer_name="Integration customer", customer_phone=phone, start_at=at),
            promotion_code=code, require_availability=False)


async def transition(database, booking_id, status):
    async with database() as session:
        booking = await session.get(Booking, booking_id)
        await PromotionService().sync_first_visit_status(session, booking, status)
        booking.status = status
        await session.commit()


@pytest.mark.anyio
@pytest.mark.parametrize("existing", [True, False])
async def test_concurrent_canonical_customer_across_masters(database, existing):
    pairs, promotion_id = await seed(database, customer=existing)
    bookings = await asyncio.wait_for(asyncio.gather(
        create(database, pairs[0], phone="+380 (50) 123-45-67"),
        create(database, pairs[1], phone=PHONE),
    ), timeout=20)
    assert len({b.customer_id for b in bookings}) == 1
    assert sorted(b.promotion_discount_amount for b in bookings) == [0, 200]
    assert sum(b.first_visit_customer_id is not None for b in bookings) == 1
    assert [b.promotion_id for b in bookings if b.promotion_id] == [promotion_id]
    async with database() as session:
        assert len((await session.scalars(select(Customer))).all()) == 1
        # The database itself also protects against a bypass of the service lock.
        other = await session.get(Booking, next(b.id for b in bookings if not b.first_visit_customer_id))
        other.first_visit_customer_id = bookings[0].customer_id
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.anyio
@pytest.mark.parametrize("released_status", [BookingStatus.cancelled, BookingStatus.no_show])
async def test_release_then_completion_is_durable(database, released_status):
    pairs, _ = await seed(database)
    first = await create(database, pairs[0])
    await transition(database, first.id, released_status)
    second = await create(database, pairs[1])
    assert second.promotion_discount_amount == 200
    await transition(database, second.id, BookingStatus.completed)
    # Status corrections must never erase the canonical completion marker.
    await transition(database, second.id, BookingStatus.cancelled)
    third = await create(database, pairs[0], at=AT + timedelta(hours=2))
    assert third.promotion_id is None
    assert third.total_amount == 1000
    async with database() as session:
        customer = await session.get(Customer, third.customer_id)
        assert customer.first_visit_completed_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize("history", [
    {"imported_last_visit_at": AT - timedelta(days=900)},
    {"imported_total_spent": 500},
])
async def test_imported_history_without_bookings_blocks_offer(database, history):
    pairs, _ = await seed(database, **history)
    booking = await create(database, pairs[1])
    assert booking.promotion_id is None
    assert booking.total_amount == 1000


@pytest.mark.anyio
async def test_legacy_completed_history_by_phone_across_masters(database):
    pairs, _ = await seed(database)
    async with database() as session:
        session.add(Booking(master_id=pairs[0][0], service_id=pairs[0][1], customer_id=None,
                            customer_name="Imported booking", customer_phone=PHONE,
                            start_at=AT-timedelta(days=30), end_at=AT-timedelta(days=30)+timedelta(minutes=30),
                            status=BookingStatus.completed))
        await session.commit()
    booking = await create(database, pairs[1])
    assert booking.promotion_id is None


@pytest.mark.anyio
async def test_live_percent_and_explicit_code_precedence_preserve_snapshot(database):
    pairs, promotion_id = await seed(database)
    first = await create(database, pairs[0])
    async with database() as session:
        promotion = await session.get(Promotion, promotion_id)
        promotion.discount_percent = 35
        session.add(Promotion(code="TEN", name_uk="Код", name_en="Code", discount_percent=10,
                              eligibility_type=PromotionEligibilityType.all_customers))
        await session.commit()
    second = await create(database, pairs[1], phone="+380501234568")
    coded = await create(database, pairs[0], phone="+380501234569", at=AT+timedelta(hours=1), code="TEN")
    assert second.promotion_discount_amount == 350
    assert coded.promotion_discount_amount == 100
    assert coded.promotion_code_snapshot == "TEN"
    assert coded.first_visit_customer_id is None
    async with database() as session:
        promotion = await session.get(Promotion, promotion_id)
        promotion.is_active = False
        await session.commit()
        original = await session.get(Booking, first.id)
        assert original.total_amount == 800
        assert original.promotion_discount_percent_snapshot == 20
    regular = await create(database, pairs[1], phone="+380501234570", at=AT+timedelta(hours=1))
    assert regular.total_amount == 1000


@pytest.mark.anyio
@pytest.mark.parametrize("restriction", ["master", "service", "future", "expired", "private"])
async def test_automatic_offer_scope_and_date_restrictions(database, restriction):
    pairs, promotion_id = await seed(database)
    async with database() as session:
        promotion = await session.get(Promotion, promotion_id)
        if restriction == "master":
            promotion.applies_to_all_masters = False
        elif restriction == "service":
            promotion.applies_to_all_services = False
        elif restriction == "future":
            promotion.starts_at = AT + timedelta(days=1)
        elif restriction == "expired":
            promotion.ends_at = AT - timedelta(days=1)
        else:
            promotion.is_public = False
        await session.commit()
    booking = await create(database, pairs[1])
    assert booking.promotion_id is None
    assert booking.total_amount == 1000


@pytest.mark.anyio
async def test_quote_contract_is_read_only_and_does_not_disclose_history(database):
    from app.api.v1.routes.bookings import quote_public_booking
    from app.schemas.booking import PublicBookingQuoteRequest

    pairs, promotion_id = await seed(database, customer=False)
    async with database() as session:
        request = dict(master_id=pairs[0][0], service_id=pairs[0][1], start_at=AT)
        anonymous = await quote_public_booking(PublicBookingQuoteRequest(**request), session)
        assert anonymous.subtotal_amount == anonymous.total_amount == 1000
        assert anonymous.eligibility.status == "customer_required"
        quoted = await quote_public_booking(PublicBookingQuoteRequest(**request, customer_phone=PHONE), session)
        assert quoted.model_dump()["subtotal_amount"] == 1000
        assert quoted.total_amount == 800
        assert quoted.discount_amount == 200
        assert quoted.applied_promotion.id == promotion_id
        assert quoted.applied_promotion.code is None
        assert quoted.applied_promotion.application_mode == "automatic"
        assert quoted.eligibility.status == "applied"
        assert not session.new and not session.dirty
        assert not (await session.scalars(select(Customer))).all()
        assert not (await session.scalars(select(Booking))).all()
    booking = await create(database, pairs[0])
    async with database() as session:
        reserved = await quote_public_booking(PublicBookingQuoteRequest(**request, customer_phone=PHONE), session)
    await transition(database, booking.id, BookingStatus.completed)
    async with database() as session:
        returning = await quote_public_booking(PublicBookingQuoteRequest(**request, customer_phone=PHONE), session)
        assert returning.model_dump() == reserved.model_dump()
        assert returning.total_amount == 1000
        assert returning.eligibility.status == "not_available"
        assert set(returning.model_dump()) == {"subtotal_amount", "applied_promotion", "discount_amount", "total_amount", "eligibility"}


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["date", "master"])
async def test_reschedule_revalidates_scope_and_keeps_snapshots(database, change):
    from sqlalchemy.orm import selectinload
    from app.models.booking import BookingServiceItem

    pairs, promotion_id = await seed(database)
    booking = await create(database, pairs[0])
    async with database() as session:
        promotion = await session.get(Promotion, promotion_id)
        promotion.discount_percent = 45
        promotion.is_active = False
        if change == "date":
            promotion.ends_at = AT + timedelta(hours=2)
        else:
            promotion.applies_to_all_masters = False
        await session.commit()
        item = (await session.execute(select(Booking).where(Booking.id == booking.id).options(
            selectinload(Booking.service_items).selectinload(BookingServiceItem.service),
            selectinload(Booking.service),
        ))).scalar_one()
        if change == "date":
            item.start_at += timedelta(days=1)
            item.end_at += timedelta(days=1)
        with pytest.raises(HTTPException) as error:
            await PromotionService().revalidate_first_visit_booking(session, item)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "promotion_no_longer_applicable"
        await session.rollback()
    async with database() as session:
        item = await session.get(Booking, booking.id)
        assert item.start_at == AT
        assert item.total_amount == 800
        assert item.promotion_discount_percent_snapshot == 20
        assert item.first_visit_customer_id == item.customer_id


@pytest.mark.anyio
async def test_quote_explicit_code_wins_without_reserving_entitlement(database):
    from app.api.v1.routes.bookings import quote_public_booking
    from app.schemas.booking import PublicBookingQuoteRequest

    pairs, _ = await seed(database)
    async with database() as session:
        session.add(Promotion(code="TEN", name_uk="Код", name_en="Code", discount_percent=10,
                              eligibility_type=PromotionEligibilityType.all_customers))
        await session.commit()
        result = await quote_public_booking(PublicBookingQuoteRequest(
            master_id=pairs[0][0], service_id=pairs[0][1], start_at=AT,
            customer_phone=PHONE, promotionCode="TEN"), session)
        assert result.total_amount == 900
        assert result.applied_promotion.code == "TEN"
        assert result.applied_promotion.application_mode == "code"
        assert not session.new and not session.dirty
        assert not (await session.scalars(select(Booking))).all()


@pytest.mark.anyio
async def test_valid_reschedule_preserves_reserved_entitlement_and_disabled_offer_snapshot(database):
    from sqlalchemy.orm import selectinload
    from app.models.booking import BookingServiceItem

    pairs, promotion_id = await seed(database)
    booking = await create(database, pairs[0])
    async with database() as session:
        promotion = await session.get(Promotion, promotion_id)
        promotion.discount_percent = 45
        promotion.is_active = False
        await session.commit()
        item = (await session.execute(select(Booking).where(Booking.id == booking.id).options(
            selectinload(Booking.service_items).selectinload(BookingServiceItem.service),
            selectinload(Booking.service),
        ))).scalar_one()
        item.start_at += timedelta(hours=1)
        item.end_at += timedelta(hours=1)
        await PromotionService().revalidate_first_visit_booking(session, item)
        await session.commit()
    async with database() as session:
        item = await session.get(Booking, booking.id)
        assert item.start_at == AT + timedelta(hours=1)
        assert item.total_amount == 800
        assert item.promotion_discount_percent_snapshot == 20
        assert item.first_visit_customer_id == item.customer_id


@pytest.mark.anyio
@pytest.mark.parametrize("inactive_status", [BookingStatus.cancelled, BookingStatus.no_show])
async def test_inactive_edits_do_not_reacquire_entitlement_and_reactivation_checks_holder(database, inactive_status):
    pairs, _ = await seed(database)
    first = await create(database, pairs[0])
    await transition(database, first.id, inactive_status)
    replacement = await create(database, pairs[1])
    async with database() as session:
        booking = (await session.execute(select(Booking).where(Booking.id == first.id).options(
            selectinload(Booking.service_items).selectinload(BookingServiceItem.service),
            selectinload(Booking.service),
        ))).scalar_one()
        customer = await session.get(Customer, booking.customer_id)
        haircut = await session.get(BarberService, pairs[0][1])
        await PromotionService().revalidate_first_visit_booking(session, booking)
        assert booking.first_visit_customer_id is None
        await PromotionService().apply_to_booking(
            session, booking=booking, customer=customer, services=[haircut],
            promotion_code=None, at=booking.start_at, preserve_existing=True,
            service_prices={haircut.id: 1200})
        assert booking.first_visit_customer_id is None
        assert booking.total_amount == 960
        await session.commit()
        with pytest.raises(HTTPException) as error:
            await PromotionService().sync_first_visit_status(session, booking, BookingStatus.confirmed)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "entitlement_reserved"
        await session.rollback()
    async with database() as session:
        holder = await session.get(Booking, replacement.id)
        assert holder.first_visit_customer_id == holder.customer_id


@pytest.mark.anyio
async def test_another_completed_visit_prevents_discounted_completion(database):
    pairs, _ = await seed(database)
    discounted = await create(database, pairs[0])
    regular = await create(database, pairs[1])
    assert regular.promotion_id is None
    await transition(database, regular.id, BookingStatus.completed)
    with pytest.raises(HTTPException) as error:
        await transition(database, discounted.id, BookingStatus.completed)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "not_eligible"
    async with database() as session:
        item = await session.get(Booking, discounted.id)
        assert item.status == BookingStatus.confirmed


@pytest.mark.anyio
async def test_authorized_manual_discount_survives_service_price_change(database):
    pairs, _ = await seed(database)
    original = await create(database, pairs[0])
    async with database() as session:
        booking = await session.get(Booking, original.id)
        customer = await session.get(Customer, booking.customer_id)
        haircut = await session.get(BarberService, pairs[0][1])
        booking.manual_discount_amount = 100
        await PromotionService().apply_to_booking(
            session, booking=booking, customer=customer, services=[haircut],
            promotion_code=None, at=booking.start_at, preserve_existing=True,
            service_prices={haircut.id: 1500})
        assert booking.manual_discount_amount == 100
        assert booking.promotion_discount_amount == 300
        assert booking.total_amount == 1100
        await session.commit()


@pytest.mark.anyio
async def test_admin_completed_price_correction_preserves_consumed_percent_and_manual(database):
    from types import SimpleNamespace
    from app.api.v1.routes.bookings import admin_update_booking
    from app.schemas.booking import AdminBookingUpdate

    pairs, promotion_id = await seed(database)
    original = await create(database, pairs[0])
    await transition(database, original.id, BookingStatus.completed)
    async with database() as session:
        booking = await session.get(Booking, original.id)
        booking.manual_discount_amount = 100
        promotion = await session.get(Promotion, promotion_id)
        promotion.discount_percent = 45
        promotion.is_active = False
        await session.commit()
        result = await admin_update_booking(
            original.id, AdminBookingUpdate(service_prices=[{"service_id": pairs[0][1], "price_amount": 1500}]),
            current_user=SimpleNamespace(is_superuser=True), session=session)
        assert result.status == BookingStatus.completed
        assert result.promotion_discount_percent == 20
        assert booking.manual_discount_amount == 100
        assert booking.promotion_discount_amount == 300
        assert result.total_amount == 1100
        customer = await session.get(Customer, original.customer_id)
        assert customer.first_visit_completed_at is not None


@pytest.mark.anyio
async def test_admin_explicit_null_removes_automatic_without_reselecting_preserving_manual(database):
    from types import SimpleNamespace
    from app.api.v1.routes.bookings import admin_update_booking
    from app.schemas.booking import AdminBookingUpdate

    pairs, _ = await seed(database)
    original = await create(database, pairs[0])
    async with database() as session:
        booking = await session.get(Booking, original.id)
        booking.manual_discount_amount = 100
        await session.commit()
        result = await admin_update_booking(
            original.id, AdminBookingUpdate(promotion_code=None),
            current_user=SimpleNamespace(is_superuser=True), session=session)
        assert booking.promotion_discount_amount == 0
        assert result.promotion_discount_percent is None
        assert booking.manual_discount_amount == 100
        assert result.total_amount == 900
        booking = await session.get(Booking, original.id)
        assert booking.promotion_id is None
        assert booking.first_visit_customer_id is None
    replacement = await create(database, pairs[1])
    assert replacement.promotion_discount_amount == 200


@pytest.mark.anyio
async def test_public_offer_metadata_and_catalog_keep_regular_prices_and_safe_terms(database):
    from app.api.v1.routes.bookings import list_public_booking_promotions, list_public_service_catalog

    pairs, promotion_id = await seed(database, imported_total_spent=500)
    async with database() as session:
        offers = await list_public_booking_promotions(session=session)
        assert len(offers) == 1
        offer = offers[0]
        assert offer.id == promotion_id
        assert offer.code is None
        assert offer.requires_code is False
        assert offer.conditional is True
        assert offer.application_mode == "automatic"
        assert offer.eligibility_type == "first_visit"
        assert offer.eligibility_scope == "barbershop"
        assert offer.applies_to_all_masters and offer.applies_to_all_services
        assert offer.master_ids == offer.base_service_ids == []
        assert not {"customer_id", "customer_phone", "first_visit_completed_at", "imported_total_spent", "imported_last_visit_at"}.intersection(offer.model_dump())
        catalog = await list_public_service_catalog(session=session)
        assert len(catalog) == 1
        assert catalog[0].price == 1000
        assert catalog[0].barber_ids == [pair[0] for pair in pairs]
        assert catalog[0].active_promotion.code is None
        assert catalog[0].active_promotion.requires_code is False
        assert catalog[0].active_promotion.conditional is True
        assert catalog[0].active_promotion.promotional_price == 800
        assert catalog[0].active_promotion.eligibility_scope == "barbershop"


@pytest.mark.anyio
async def test_legacy_local_phone_import_blocks_international_first_visit(database):
    pairs, _ = await seed(database, customer=False)
    async with database() as session:
        customer = Customer(phone="+0501234567", name="Legacy imported", imported_total_spent=500)
        session.add(customer)
        await session.commit()
        legacy_id = customer.id
    booking = await create(database, pairs[0])
    assert booking.customer_id == legacy_id
    assert booking.customer_phone == PHONE
    assert booking.promotion_id is None
    assert booking.total_amount == 1000


@pytest.mark.anyio
async def test_completed_history_on_duplicate_local_alias_blocks_primary_customer(database):
    pairs, _ = await seed(database)
    async with database() as session:
        primary = (await session.scalars(select(Customer).where(Customer.phone == PHONE))).one()
        alias = Customer(phone="+0501234567", name="Legacy duplicate")
        session.add(alias)
        await session.flush()
        session.add(Booking(master_id=pairs[0][0], service_id=pairs[0][1], customer_id=alias.id,
                            customer_name="Legacy duplicate", customer_phone=alias.phone,
                            start_at=AT-timedelta(days=30), end_at=AT-timedelta(days=30)+timedelta(minutes=30),
                            status=BookingStatus.completed))
        await session.commit()
        primary_id = primary.id
    booking = await create(database, pairs[1])
    assert booking.customer_id == primary_id
    assert booking.promotion_id is None
    assert booking.total_amount == 1000


@pytest.mark.anyio
async def test_concurrent_local_and_international_identity_share_one_entitlement(database):
    pairs, _ = await seed(database, customer=False)
    bookings = await asyncio.wait_for(asyncio.gather(
        create(database, pairs[0], phone="050 123 45 67"),
        create(database, pairs[1], phone=PHONE),
    ), timeout=20)
    assert len({booking.customer_id for booking in bookings}) == 1
    assert {booking.customer_phone for booking in bookings} == {PHONE}
    assert sorted(booking.promotion_discount_amount for booking in bookings) == [0, 200]
    assert sum(booking.first_visit_customer_id is not None for booking in bookings) == 1
    async with database() as session:
        customers = (await session.scalars(select(Customer))).all()
        assert len(customers) == 1
        assert customers[0].phone == PHONE


@pytest.mark.anyio
async def test_reservation_on_newer_legacy_alias_blocks_older_primary_customer(database):
    pairs, promotion_id = await seed(database)
    async with database() as session:
        primary = (await session.scalars(select(Customer).where(Customer.phone == PHONE))).one()
        alias = Customer(phone="+0501234567", name="Legacy duplicate")
        session.add(alias)
        await session.flush()
        session.add(Booking(master_id=pairs[0][0], service_id=pairs[0][1], customer_id=alias.id,
                            first_visit_customer_id=alias.id, promotion_id=promotion_id,
                            customer_name="Legacy duplicate", customer_phone=alias.phone,
                            start_at=AT, end_at=AT+timedelta(minutes=30), status=BookingStatus.confirmed,
                            promotion_discount_percent_snapshot=20,
                            promotion_eligibility_type_snapshot="first_visit",
                            promotion_application_mode_snapshot="automatic",
                            subtotal_amount=1000, promotion_discount_amount=200, total_amount=800))
        await session.commit()
        primary_id = primary.id
    booking = await create(database, pairs[1])
    assert booking.customer_id == primary_id
    assert booking.promotion_id is None
    assert booking.first_visit_customer_id is None
    assert booking.total_amount == 1000


@pytest.mark.anyio
@pytest.mark.parametrize("submitted", [PHONE, "050 123 45 67"])
async def test_booking_uses_exact_phone_owner_even_when_alias_is_older(database, submitted):
    pairs, _ = await seed(database, customer=False)
    exact_phone = PHONE if submitted == PHONE else "+0501234567"
    older_phone = "+0501234567" if submitted == PHONE else PHONE
    async with database() as session:
        older = Customer(phone=older_phone, name="Older alias owner")
        session.add(older)
        await session.flush()
        exact = Customer(phone=exact_phone, name="Exact account owner")
        session.add(exact)
        await session.commit()
        exact_id = exact.id
    booking = await create(database, pairs[0], phone=submitted)
    assert booking.customer_id == exact_id
    assert booking.customer_phone == PHONE
    async with database() as session:
        assert len((await session.scalars(select(Customer))).all()) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("submitted,stored_phone,duplicate", [
    ("050 123 45 67", PHONE, False),
    (PHONE, "+0501234567", False),
    ("050 123 45 67", "+0501234567", True),
    (PHONE, PHONE, True),
])
async def test_verified_otp_preserves_exact_owner_and_uses_existing_alias_fallback(database, submitted, stored_phone, duplicate):
    from datetime import UTC
    from app.core.security import hash_otp_code
    from app.models.customer_otp_code import CustomerOtpCode
    from app.services.customer_auth import CustomerAuthService

    auth = CustomerAuthService()
    normalized = auth.normalize_phone(submitted)
    now = datetime.now(UTC)
    async with database() as session:
        if duplicate:
            other_phone = PHONE if stored_phone == "+0501234567" else "+0501234567"
            session.add(Customer(phone=other_phone, name="Older alias account"))
            await session.flush()
        intended = Customer(phone=stored_phone, name="Intended account")
        session.add(intended)
        # Seed a known OTP directly; never call request_otp or any sender.
        challenge = CustomerOtpCode(phone=normalized, code_hash=hash_otp_code(normalized, "123456"),
                                    sent_at=now, expires_at=now+timedelta(minutes=10))
        session.add(challenge)
        await session.commit()
        intended_id = intended.id
        result = await auth.verify_otp(session, submitted, "123456")
        assert result.customer.id == intended_id
        assert result.customer.phone == stored_phone
        assert result.is_new_customer is False
        assert result.customer.phone_verified_at is not None
        assert challenge.verified_at is not None
        assert len((await session.scalars(select(Customer))).all()) == (2 if duplicate else 1)
