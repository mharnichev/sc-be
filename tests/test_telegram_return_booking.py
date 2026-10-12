"""Returning clients prefill a new Telegram draft, with scoped offer attribution."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.routes import messaging as ui
from app.models.booking import BarberService, Booking, BookingSource, BookingStatus, Master
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, TelegramBotSession, TelegramContact
from app.models.repeat_booking import RepeatBookingEvent, RepeatBookingEventType, RepeatBookingOffer, RepeatBookingOfferStatus
from app.models.telegram_booking_funnel import TelegramBookingFunnelEvent
from app.services.repeat_booking import repeat_booking_service
from app.services.telegram_return_booking import (
    attribute_telegram_repeat_booking, dispatch_telegram_return_booking,
    is_telegram_repeat_action, validate_telegram_repeat_booking,
)
from tests.test_repeat_booking_integration import scenario, TOKEN  # noqa: F401
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


@pytest.fixture
async def contact(database, scenario):
    async with database() as session:
        contact = TelegramContact(chat_id="return-client", telegram_user_id="123",
                                  phone="+380500000001", linked_customer_id=scenario["customer_id"])
        session.add(contact)
        await session.flush()
        draft = TelegramBotSession(chat_id=contact.chat_id, telegram_contact_id=contact.id,
            linked_customer_id=contact.linked_customer_id, state="booked",
            selected_master_id=scenario["master_id"], selected_service_id=scenario["service_ids"][0],
            payload_json={"telegram_funnel_attempt_id": "9db8aefb-adac-4b38-90d1-b1e30ed347cb",
                          "selected_visit_time": scenario["at"].isoformat(), "selected_visit_date": scenario["at"].date().isoformat(),
                          "confirmation_token": "stale", "slot_token": "old", "booking_id": scenario["source_id"],
                          "booking_recovery_id": 7})
        session.add(draft)
        await session.commit()
        return contact.id


async def dispatch(database, contact_id, text):
    provider = SimpleNamespace(send_message=AsyncMock(), answer_callback_query=AsyncMock())
    async with database() as session:
        contact = await session.get(TelegramContact, contact_id)
        result = await dispatch_telegram_return_booking(provider, session, chat_id=contact.chat_id,
            text=text, telegram_contact=contact, callback_query_id="callback")
        await session.commit()
        return result, provider


def test_repeat_action_recognizes_manual_source_and_capability():
    assert is_telegram_repeat_action("Записатися знову")
    assert is_telegram_repeat_action("repeat_booking:17")
    assert is_telegram_repeat_action(f"repeat_offer:{TOKEN}")
    assert len(f"repeat_offer:{TOKEN}".encode()) <= 64
    assert not is_telegram_repeat_action("Новий запис")


@pytest.mark.anyio
async def test_manual_repeat_replaces_terminal_draft_and_records_new_attempt(database, scenario, contact):
    async with database() as session:
        services = list(await session.scalars(select(BarberService).order_by(BarberService.id)))
        services[0].price, services[1].price = 1200, 600
        services[0].duration_minutes, services[1].duration_minutes = 40, 20
        await session.commit()
    result, provider = await dispatch(database, contact, "Записатися знову")
    assert result["action"] == "repeat_booking_start"
    async with database() as session:
        draft = await session.scalar(select(TelegramBotSession))
        assert draft.telegram_contact_id == contact
        assert draft.linked_customer_id == scenario["customer_id"]
        assert draft.selected_master_id == scenario["master_id"]
        assert draft.payload_json["selected_service_ids"] == scenario["service_ids"]
        assert UUID(draft.payload_json["telegram_funnel_attempt_id"])
        assert draft.payload_json["telegram_funnel_attempt_id"] != "9db8aefb-adac-4b38-90d1-b1e30ed347cb"
        assert not ({"selected_visit_time", "selected_visit_date", "confirmation_token", "slot_token",
                     "booking_id", "booking_recovery_id", "repeat_booking_offer_id"} & draft.payload_json.keys())
        assert draft.state == "selecting_date"
        assert await session.scalar(select(func.count(Booking.id))) == 1
        assert await session.scalar(select(func.count(RepeatBookingOffer.id))) == 1
        steps = list(await session.scalars(select(TelegramBookingFunnelEvent.step)))
        assert sorted(steps) == sorted(["booking_start", "contact_entered", "master_selected", "service_selected"])
    bodies = [call.kwargs["body"] for call in provider.send_message.await_args_list]
    assert any("1800 грн" in body and "60 хв" in body for body in bodies)
    assert any("Оберіть дату" in body for body in bodies)


@pytest.mark.anyio
@pytest.mark.parametrize("inactive", ["master", "service"])
async def test_disabled_exact_context_explains_fallback_and_clears_stale_state(database, scenario, contact, monkeypatch, inactive):
    master_menu = AsyncMock()
    monkeypatch.setattr(ui, "_send_master_list", master_menu)
    async with database() as session:
        item = await session.get(Master if inactive == "master" else BarberService,
                                 scenario["master_id"] if inactive == "master" else scenario["service_ids"][1])
        item.is_active = False
        await session.commit()
    result, provider = await dispatch(database, contact, f'repeat_booking:{scenario["source_id"]}')
    assert result["action"] == "repeat_booking_fallback"
    master_menu.assert_awaited_once()
    assert "недоступні" in provider.send_message.await_args.kwargs["body"]
    async with database() as session:
        draft = await session.scalar(select(TelegramBotSession))
        assert draft.selected_master_id is None
        assert set(draft.payload_json) == {"telegram_funnel_attempt_id"}


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["foreign_source", "foreign_offer", "expired_offer", "consumed_offer", "malformed_source", "out_of_range_source"])
async def test_unowned_or_stale_repeat_does_not_change_draft_or_create_booking(database, scenario, contact, entry):
    text = f'repeat_booking:{scenario["source_id"]}'
    async with database() as session:
        if entry.startswith("foreign"):
            other = Customer(name="Other", phone="+380500000999")
            session.add(other)
            await session.flush()
            (await session.get(TelegramContact, contact)).linked_customer_id = other.id
        if entry.endswith("offer"):
            text = f"repeat_offer:{TOKEN}"
        offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
        if entry == "expired_offer":
            offer.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if entry == "consumed_offer":
            offer.status = RepeatBookingOfferStatus.booked
            offer.token_hash = None
        if entry == "malformed_source":
            text = "repeat_booking:nope"
        if entry == "out_of_range_source":
            text = "repeat_booking:2147483648"
        await session.commit()
    result, _ = await dispatch(database, contact, text)
    assert result["action"] in {"repeat_booking_unavailable", "repeat_booking_offer_unavailable"}
    async with database() as session:
        draft = await session.scalar(select(TelegramBotSession))
        assert draft.state == "booked"
        assert draft.payload_json["confirmation_token"] == "stale"
        assert await session.scalar(select(func.count(Booking.id))) == 1


@pytest.mark.anyio
async def test_capability_entry_consumes_only_after_normal_booking_and_records_actual_completion(database, scenario, contact):
    result, _ = await dispatch(database, contact, f"repeat_offer:{TOKEN}")
    assert result["action"] == "repeat_booking_start"
    async with database() as session:
        draft = await session.scalar(select(TelegramBotSession))
        assert draft.payload_json["repeat_booking_offer_id"] == scenario["offer_id"]
        assert TOKEN not in str(draft.payload_json)
        assert await validate_telegram_repeat_booking(session, draft)
        # The normal booking service owns all price, duration, schedule and availability checks.
        created = Booking(master_id=scenario["master_id"], service_id=scenario["service_ids"][0],
            customer_id=scenario["customer_id"], customer_name="Repeat customer", customer_phone="+380500000001",
            start_at=scenario["at"], end_at=scenario["at"] + timedelta(minutes=45),
            status=BookingStatus.confirmed, source=BookingSource.telegram)
        session.add(created)
        await session.flush()
        offer = await attribute_telegram_repeat_booking(session, draft, created,
            requested_master_id=scenario["master_id"], requested_service_ids=scenario["service_ids"])
        assert offer.result_booking_id == created.id
        assert offer.status == RepeatBookingOfferStatus.booked
        assert offer.token_hash is None
        assert not await repeat_booking_service.mark_repeat_visit_completed(session, created)
        await session.commit()
        with pytest.raises(HTTPException) as exc:
            await validate_telegram_repeat_booking(session, draft)
        assert exc.value.status_code == 401
        created.status = BookingStatus.completed
        created.completed_at = datetime.now(UTC)
        assert await repeat_booking_service.mark_repeat_visit_completed(session, created)
        assert not await repeat_booking_service.mark_repeat_visit_completed(session, created)
        await session.commit()
        kinds = list(await session.scalars(select(RepeatBookingEvent.event_type)))
        assert sorted(kinds) == sorted([RepeatBookingEventType.link_opened.value,
                                      RepeatBookingEventType.booking_started.value,
                                      RepeatBookingEventType.booking_completed.value])


@pytest.mark.anyio
async def test_manual_repeat_attribution_is_noop_with_no_database_work():
    session = SimpleNamespace()
    draft = SimpleNamespace(payload_json={})
    assert await validate_telegram_repeat_booking(session, draft) is None
    assert await attribute_telegram_repeat_booking(session, draft, object(),
        requested_master_id=1, requested_service_ids=[1]) is None


@pytest.mark.anyio
async def test_manual_repeat_uses_latest_completed_visit_but_exact_id_can_choose_older(database, scenario, contact):
    async with database() as session:
        later = Booking(master_id=scenario["master_id"], service_id=scenario["service_ids"][1],
            customer_id=scenario["customer_id"], customer_name="Later", customer_phone="+380500000001",
            start_at=datetime.now(UTC) - timedelta(days=1, hours=1),
            end_at=datetime.now(UTC) - timedelta(days=1), completed_at=datetime.now(UTC) - timedelta(days=1),
            status=BookingStatus.completed)
        session.add(later)
        await session.commit()
    assert (await dispatch(database, contact, "Записатися знову"))[0]["action"] == "repeat_booking_start"
    async with database() as session:
        draft = await session.scalar(select(TelegramBotSession))
        assert draft.payload_json["selected_service_ids"] == scenario["service_ids"][1:]
    assert (await dispatch(database, contact, f'repeat_booking:{scenario["source_id"]}'))[0]["action"] == "repeat_booking_start"
    async with database() as session:
        draft = await session.scalar(select(TelegramBotSession))
        assert draft.payload_json["selected_service_ids"] == scenario["service_ids"]
        assert await session.scalar(select(func.count(RepeatBookingOffer.id))) == 1


@pytest.mark.anyio
async def test_scheduled_offer_primary_button_enters_telegram_and_keeps_web_option(database, scenario):
    async with database() as session:
        preference = ClientCommunicationPreference(customer_id=scenario["customer_id"], telegram_chat_id="return-client")
        session.add(preference)
        offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
        offer.status = RepeatBookingOfferStatus.scheduled
        offer.sent_at = None
        offer.scheduled_at = scenario["now"] - timedelta(minutes=1)
        provider = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(provider_message_id="offer")))
        from app.services.repeat_booking import RepeatBookingService
        service = RepeatBookingService(provider=provider, now=lambda: scenario["now"])
        # Delivery eligibility/quiet-hour policies are independently covered by scheduler tests.
        service.eligibility_reason = AsyncMock(return_value=None)
        from unittest.mock import patch
        with patch.object(type(service.messaging), "adjust_for_quiet_hours", return_value=scenario["now"]):
            assert await service.send_offer(session, offer)
        buttons = provider.send_message.await_args.kwargs["reply_markup"]["inline_keyboard"]
        assert buttons[0][0]["text"] == "Записатися знову"
        callback = buttons[0][0]["callback_data"]
        assert len(callback.encode()) <= 64
        assert callback.startswith("repeat_offer:")
        assert offer.token_hash == service.hash_token(callback.removeprefix("repeat_offer:"))
        assert buttons[1][0]["url"].endswith("#" + callback.removeprefix("repeat_offer:"))


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["Записатися знову", "explicit_source"])
async def test_completed_archive_is_valid_repeat_history_under_operational_session(database, scenario, contact, entry):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.core.database import OperationalSession
    async with database() as session:
        source = await session.get(Booking, scenario["source_id"])
        source.archived_at = datetime.now(UTC)
        await session.commit()
    operational_database = async_sessionmaker(bind=database.kw["bind"], class_=AsyncSession,
                                               sync_session_class=OperationalSession, expire_on_commit=False)
    async with operational_database() as session:
        assert await session.scalar(select(Booking.id)) is None
        assert await session.scalar(select(Booking.id).execution_options(include_archived_bookings=True)) == scenario["source_id"]
    text = f'repeat_booking:{scenario["source_id"]}' if entry == "explicit_source" else entry
    result, _ = await dispatch(operational_database, contact, text)
    assert result["action"] == "repeat_booking_start"
    async with operational_database() as session:
        draft = await session.scalar(select(TelegramBotSession))
        assert draft.selected_master_id == scenario["master_id"]
        assert draft.payload_json["selected_service_ids"] == scenario["service_ids"]
        assert await session.scalar(select(func.count(Booking.id)).execution_options(include_archived_bookings=True)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("channel", ["telegram", "web"])
async def test_archived_source_preserves_existing_capability_context_and_consumption(database, scenario, contact, channel):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.core.database import OperationalSession
    async with database() as session:
        source = await session.get(Booking, scenario["source_id"])
        source.archived_at = datetime.now(UTC)
        await session.commit()
    operational_database = async_sessionmaker(bind=database.kw["bind"], class_=AsyncSession,
                                               sync_session_class=OperationalSession, expire_on_commit=False)
    if channel == "telegram":
        result, _ = await dispatch(operational_database, contact, f"repeat_offer:{TOKEN}")
        assert result["action"] == "repeat_booking_start"
    async with operational_database() as session:
        if channel == "web":
            context = await repeat_booking_service.context(session, TOKEN)
            assert context.can_prefill
            assert context.preferred_master.id == scenario["master_id"]
            assert [item.id for item in context.services] == scenario["service_ids"]
            await repeat_booking_service.mark_started(session, TOKEN)
        else:
            draft = await session.scalar(select(TelegramBotSession))
            offer = await validate_telegram_repeat_booking(session, draft)
            assert offer.completed_booking_id == scenario["source_id"]
            assert draft.payload_json["selected_service_ids"] == scenario["service_ids"]
        created = Booking(master_id=scenario["master_id"], service_id=scenario["service_ids"][0],
            customer_id=scenario["customer_id"], customer_name="Repeat customer", customer_phone="+380500000001",
            start_at=scenario["at"], end_at=scenario["at"] + timedelta(minutes=45),
            status=BookingStatus.confirmed,
            source=BookingSource.telegram if channel == "telegram" else BookingSource.web)
        session.add(created)
        await session.flush()
        if channel == "telegram":
            offer = await attribute_telegram_repeat_booking(session, draft, created,
                requested_master_id=scenario["master_id"], requested_service_ids=scenario["service_ids"])
        else:
            offer = await repeat_booking_service.attribute_booking(session, token=TOKEN, booking=created,
                requested_master_id=scenario["master_id"], requested_service_ids=scenario["service_ids"])
        assert offer.status == RepeatBookingOfferStatus.booked
        assert offer.result_booking_id == created.id
        assert offer.token_hash is None and offer.revoked_at is not None
        assert not await repeat_booking_service.mark_repeat_visit_completed(session, created)
        await session.commit()
        with pytest.raises(HTTPException) as exc:
            await repeat_booking_service.context(session, TOKEN)
        assert exc.value.status_code == 401
        kinds = list(await session.scalars(select(RepeatBookingEvent.event_type)))
        assert sorted(kinds) == sorted([RepeatBookingEventType.link_opened.value,
                                      RepeatBookingEventType.booking_started.value])


@pytest.mark.anyio
async def test_precreation_offer_validation_does_not_lock_and_final_attribution_refreshes_consumed_offer(database, scenario, contact):
    from sqlalchemy import event
    engine = database.kw["bind"]
    statements = []
    def record_sql(connection, cursor, statement, parameters, context, executemany):
        if "repeat_booking_offers" in statement and statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        async with database() as first:
            draft = await first.scalar(select(TelegramBotSession))
            draft.payload_json = {**draft.payload_json, "repeat_booking_offer_id": scenario["offer_id"]}
            # Keep this mutation outside the later savepoint to exercise stale identity-map reload.
            await first.commit()
            offer = await validate_telegram_repeat_booking(first, draft)
            assert offer.status == RepeatBookingOfferStatus.sent
            assert statements and all("FOR UPDATE" not in sql.upper() for sql in statements)
            async with database() as second:
                consumed = await second.get(RepeatBookingOffer, scenario["offer_id"])
                consumed.status = RepeatBookingOfferStatus.booked
                consumed.token_hash = None
                consumed.revoked_at = datetime.now(UTC)
                await second.commit()
            assert offer.status == RepeatBookingOfferStatus.sent
            statements.clear()
            with pytest.raises(HTTPException) as exc:
                async with first.begin_nested():
                    created = Booking(master_id=scenario["master_id"], service_id=scenario["service_ids"][0],
                        customer_id=scenario["customer_id"], customer_name="Repeat customer", customer_phone="+380500000001",
                        start_at=scenario["at"], end_at=scenario["at"] + timedelta(minutes=45),
                        status=BookingStatus.confirmed, source=BookingSource.telegram)
                    first.add(created)
                    await first.flush()
                    await attribute_telegram_repeat_booking(first, draft, created,
                        requested_master_id=scenario["master_id"], requested_service_ids=scenario["service_ids"])
            assert exc.value.status_code == 401
            assert any("FOR UPDATE" in sql.upper() for sql in statements)
            await first.commit()
        async with database() as check:
            assert await check.scalar(select(func.count(Booking.id))) == 1
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
