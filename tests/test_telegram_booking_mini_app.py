from datetime import UTC, datetime, timedelta
import hmac
import json
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.routes import telegram_mini_app as mini_app
from app.core.database import get_db_session
from app.models.booking import Booking
from test_inventory_integration import anyio_backend, database  # noqa: F401
from test_telegram_booking_management import setup


def signed_headers(token, user_id):
    fields = {"auth_date": str(int(datetime.now(UTC).timestamp())),
              "user": json.dumps({"id": user_id})}
    data = "\n".join(f"{name}={value}" for name, value in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    return {"X-Telegram-Init-Data": urlencode({**fields, "hash": hmac.digest(secret, data.encode(), "sha256").hex()})}


@pytest.mark.anyio
async def test_mini_app_requires_owner_and_moves_once(database, monkeypatch):
    token = "isolated-test-token"
    monkeypatch.setattr(mini_app.settings, "telegram_bot_token", token)
    monkeypatch.setattr(mini_app.settings, "telegram_booking_mini_app_url", "https://example.test/telegram/booking-app")

    async def no_waitlist(*args):
        pass

    monkeypatch.setattr(mini_app.management, "offer_freed_booking_slot", no_waitlist)
    monkeypatch.setattr(mini_app.messaging, "_schedule_booking_notifications", lambda *args: None)

    async def db():
        async with database() as session:
            yield session

    async with database() as session:
        booking, _, _, _, start = await setup(session)
        booking_id = booking.id

    app = FastAPI()
    app.include_router(mini_app.router)
    app.dependency_overrides[get_db_session] = db
    path = "/telegram/booking-app/visits"
    visit = f"{path}/{booking_id}"
    headers = signed_headers(token, 123)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test") as client:
        assert (await client.get(path, headers=signed_headers("wrong-token", 123))).status_code == 401
        assert (await client.get(visit + "/slots", params={"day": start.date()},
                                 headers=signed_headers(token, 999))).status_code == 404
        page = await client.get("/telegram/booking-app")
        assert page.status_code == 200 and "Перенесення запису" in page.text
        response = await client.get(path, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        before = response.json()["visits"][0]
        candidate = start + timedelta(hours=1)
        options = await client.get(visit + "/slots", params={"day": start.date()}, headers=headers)
        assert candidate in [datetime.fromisoformat(slot) for slot in options.json()]
        choice = {"start": candidate.isoformat(), "version": before["version"]}
        moved = await client.post(visit + "/move", headers=headers, json=choice)
        assert moved.status_code == 200, moved.text
        assert moved.json()["price"] == before["price"]
        assert (await client.post(visit + "/move", headers=headers, json=choice)).status_code == 409

    async with database() as session:
        assert (await session.get(Booking, booking_id)).start_at == candidate
