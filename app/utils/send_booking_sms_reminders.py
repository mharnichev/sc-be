from __future__ import annotations

import asyncio
import math
import os

from sentry_sdk.crons import monitor

from app.core.database import AsyncSessionLocal
from app.core.sentry import init_sentry
from app.services.booking_sms_notifications import booking_sms_notification_service

CRON_MONITOR_SLUG = "booking-sms-reminders"


def cron_monitor_config() -> dict[str, object]:
    # Mirrors the sleep of the production loop in sc-spellbook's docker-compose.prod.yml.
    try:
        interval_seconds = int(os.environ.get("BOOKING_SMS_REMINDER_INTERVAL_SECONDS") or 300)
    except ValueError:
        interval_seconds = 300
    return {
        "schedule": {"type": "interval", "value": max(1, math.ceil(interval_seconds / 60)), "unit": "minute"},
        "checkin_margin": 5,
        "max_runtime": 10,
        "timezone": "UTC",
    }


async def run() -> int:
    async with AsyncSessionLocal() as session:
        return await booking_sms_notification_service.send_due_booking_reminders(session)


def main() -> None:
    init_sentry("booking-sms-reminders")
    # Without a DSN the monitor is a no-op; with one, missed or failed runs alert in Sentry Crons.
    with monitor(monitor_slug=CRON_MONITOR_SLUG, monitor_config=cron_monitor_config()):
        sent = asyncio.run(run())
    print(f"sent={sent}")


if __name__ == "__main__":
    main()
