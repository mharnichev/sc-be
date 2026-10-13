"""Telegram catalog eligibility and stale selection recovery on real PostgreSQL.

Run with INVENTORY_TEST_DATABASE_URL pointing to a disposable local test database.
The imported fixture creates and drops a separate schema for each test; only
Telegram delivery is replaced, while catalog SQL and booking availability run live.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.routes import messaging as routes
from app.models.booking import BarberService, BaseService, Master, MasterAvailabilityWindow
from app.models.messaging import TelegramBotSession
from test_inventory_integration import anyio_backend, database  # noqa: F401
from test_telegram_booking_flow import callbacks
from test_telegram_connect import FakeTelegramMessageProvider


@pytest.fixture(autouse=True)
def isolated_delivery(monkeypatch):
    from app.services import telegram_inbox

    async def reply(*args, **kwargs):
        return None

    monkeypatch.setattr(telegram_inbox, "send_event_reply", reply)
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.sent_photos = []
    FakeTelegramMessageProvider.answered_callbacks = []


async def seed_catalog(session):
    master = Master(full_name="Catalog test master", is_active=True)
    other_master = Master(full_name="Other catalog test master", is_active=True)
    active_base = BaseService(name="Active base", price=500, duration_minutes=30, is_active=True)
    inactive_base = BaseService(name="Inactive base", price=600, duration_minutes=45, is_active=False)
    session.add_all([master, other_master, active_base, inactive_base])
    await session.flush()
    active = BarberService(master_id=master.id, base_service_id=active_base.id,
                           name="Active copied service", price=500, duration_minutes=30, is_active=True)
    inactive = BarberService(master_id=master.id, base_service_id=inactive_base.id,
                             name="Inactive base copied service", price=600, duration_minutes=45, is_active=True)
    legacy = BarberService(master_id=master.id, base_service_id=None,
                           name="Legacy free service", price=0, duration_minutes=15, is_active=True)
    disabled = BarberService(master_id=master.id, name="Disabled service",
                             price=100, duration_minutes=15, is_active=False)
    foreign = BarberService(master_id=other_master.id, name="Other master service",
                            price=100, duration_minutes=15, is_active=True)
    session.add_all([active, inactive, legacy, disabled, foreign])
    await session.flush()
    bot = TelegramBotSession(chat_id="123", selected_master_id=master.id,
                             state=routes.TELEGRAM_SELECTING_SERVICES_STATE,
                             payload_json={"selected_service_ids": [], "customer_comment": "Keep this note"})
    session.add(bot)
    await session.commit()
    return SimpleNamespace(master=master, active=active, inactive=inactive, legacy=legacy,
                           disabled=disabled, foreign=foreign, bot=bot)


def stale_dependencies():
    return {
        "selected_visit_date": "2026-11-12",
        "selected_visit_time": "2026-11-12T10:00:00+02:00",
        "slot_token": "old-slot",
        "confirmation_token": "old-confirmation",
        "booking_id": 999,
        "booking_recovery": {"token": "old-recovery"},
        "date_page": {"token": "old-page"},
    }


@pytest.mark.anyio
async def test_service_list_matches_active_base_and_legacy_booking_eligibility(database):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        catalog = await seed_catalog(session)
        assert catalog.inactive.is_active and catalog.inactive.base_service_id is not None
        assert await routes._handle_services_action(telegram, session, chat_id="123")
        markup = telegram.sent[-1][2]
        service_callbacks = {value for value in callbacks(markup) if value.startswith("select_service:")}
        assert service_callbacks == {f"select_service:{catalog.active.id}", f"select_service:{catalog.legacy.id}"}
        buttons = " ".join(button["text"] for row in markup["inline_keyboard"] for button in row)
        assert catalog.active.name in buttons and catalog.legacy.name in buttons
        assert catalog.inactive.name not in buttons
        assert "Далі" not in callbacks(markup)


@pytest.mark.anyio
@pytest.mark.parametrize("selected", ["active", "legacy"])
async def test_active_base_and_legacy_zero_price_services_can_be_selected(database, selected):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        catalog = await seed_catalog(session)
        service = getattr(catalog, selected)
        assert await routes._handle_service_selection(
            telegram, session, chat_id="123", service_id=service.id, callback_query_id="choose-service")
        await session.refresh(catalog.bot)
        assert catalog.bot.selected_service_id == service.id
        assert catalog.bot.payload_json["selected_service_ids"] == [service.id]
        assert "Далі" in callbacks(telegram.sent[-1][2])
        assert f"Загальна вартість: {service.price} грн" in telegram.sent[-1][1]


@pytest.mark.anyio
async def test_old_callback_for_inactive_base_is_rejected_and_refreshes_catalog(database):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        catalog = await seed_catalog(session)
        assert not await routes._handle_service_selection(
            telegram, session, chat_id="123", service_id=catalog.inactive.id, callback_query_id="stale-service")
        await session.refresh(catalog.bot)
        assert catalog.bot.selected_service_id is None
        assert catalog.bot.payload_json["selected_service_ids"] == []
        assert "stale-service" in telegram.answered_callbacks
        assert catalog.inactive.name not in telegram.sent[-1][1]
        assert f"select_service:{catalog.inactive.id}" not in callbacks(telegram.sent[-1][2])
        assert f"select_service:{catalog.legacy.id}" in callbacks(telegram.sent[-1][2])
        assert "Далі" not in callbacks(telegram.sent[-1][2])


@pytest.mark.anyio
async def test_catalog_refresh_prunes_only_invalid_services_and_clears_dependencies(database):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        catalog = await seed_catalog(session)
        catalog.bot.selected_service_id = catalog.inactive.id
        catalog.bot.state = routes.TELEGRAM_READY_TO_BOOK_STATE
        catalog.bot.payload_json = {
            **catalog.bot.payload_json,
            **stale_dependencies(),
            "selected_service_ids": [catalog.inactive.id, catalog.legacy.id, catalog.disabled.id,
                                     catalog.active.id, catalog.foreign.id, 999999],
        }
        await session.commit()
        assert await routes._handle_services_action(telegram, session, chat_id="123")
        await session.refresh(catalog.bot)
        assert catalog.bot.selected_service_id == catalog.legacy.id
        assert catalog.bot.state == routes.TELEGRAM_SELECTING_SERVICES_STATE
        assert catalog.bot.payload_json == {
            "selected_service_ids": [catalog.legacy.id, catalog.active.id],
            "customer_comment": "Keep this note",
        }
        body, markup = telegram.sent[-1][1:]
        assert "Загальна вартість: 500 грн" in body and "Тривалість: 45 хв" in body
        assert f"✓ {catalog.legacy.name}" in body and f"✓ {catalog.active.name}" in body
        assert "Далі" in callbacks(markup)


@pytest.mark.anyio
async def test_refresh_keeps_dependencies_when_valid_selection_is_unchanged(database):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        catalog = await seed_catalog(session)
        catalog.bot.selected_service_id = catalog.legacy.id
        payload = {**catalog.bot.payload_json, **stale_dependencies(),
                   "selected_service_ids": [catalog.legacy.id]}
        catalog.bot.payload_json = payload
        await session.commit()
        assert await routes._handle_services_action(telegram, session, chat_id="123")
        await session.refresh(catalog.bot)
        assert catalog.bot.selected_service_id == catalog.legacy.id
        assert catalog.bot.payload_json == payload
        assert "Загальна вартість: 0 грн" in telegram.sent[-1][1]


@pytest.mark.anyio
async def test_real_availability_failure_recovers_selection_and_reaches_time_picker(database):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        catalog = await seed_catalog(session)
        visit_date = datetime.now(routes.KYIV_TZ).date() + timedelta(days=2)
        while routes.booking_service_layer.is_closed_business_day(visit_date):
            visit_date += timedelta(days=1)
        start_at = datetime.combine(visit_date, datetime.min.time(), tzinfo=routes.KYIV_TZ) + timedelta(hours=10)
        session.add(MasterAvailabilityWindow(master_id=catalog.master.id, start_at=start_at,
                                             end_at=start_at + timedelta(hours=2)))
        catalog.bot.selected_service_id = catalog.inactive.id
        catalog.bot.payload_json = {
            **catalog.bot.payload_json, **stale_dependencies(),
            "selected_service_ids": [catalog.inactive.id, catalog.legacy.id],
        }
        await session.commit()
        with pytest.raises(HTTPException) as error:
            await routes._available_visit_dates(session, master_id=catalog.master.id,
                                                service_ids=catalog.bot.payload_json["selected_service_ids"])
        assert error.value.status_code == 404
        assert not await routes._handle_date_time_action(telegram, session, chat_id="123")
        await session.refresh(catalog.bot)
        assert catalog.bot.selected_service_id == catalog.legacy.id
        assert catalog.bot.payload_json == {
            "selected_service_ids": [catalog.legacy.id], "customer_comment": "Keep this note"}
        assert catalog.bot.state == routes.TELEGRAM_SELECTING_SERVICES_STATE
        assert catalog.inactive.name not in telegram.sent[-2][1]
        assert "Далі" in callbacks(telegram.sent[-2][2])
        assert await routes._handle_date_time_action(telegram, session, chat_id="123")
        assert catalog.bot.state == routes.TELEGRAM_SELECTING_DATE_STATE
        assert f"select_date:{visit_date.isoformat()}" in callbacks(telegram.sent[-1][2])
        assert await routes._handle_date_selection(telegram, session, chat_id="123",
                                                  visit_date=visit_date, callback_query_id="choose-date")
        await session.refresh(catalog.bot)
        assert catalog.bot.state == routes.TELEGRAM_SELECTING_TIME_STATE
        assert catalog.bot.payload_json["selected_service_ids"] == [catalog.legacy.id]
        assert catalog.bot.payload_json["selected_visit_date"] == visit_date.isoformat()
        token = catalog.bot.payload_json["slot_token"]
        assert any(value.startswith("select_time:") and value.endswith(f"|{token}")
                   for value in callbacks(telegram.sent[-1][2]))
