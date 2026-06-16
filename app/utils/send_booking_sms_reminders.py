from __future__ import annotations

import asyncio

from app.core.database import AsyncSessionLocal
from app.services.booking_sms_notifications import booking_sms_notification_service


async def run() -> int:
    async with AsyncSessionLocal() as session:
        return await booking_sms_notification_service.send_due_booking_reminders(session)


def main() -> None:
    sent = asyncio.run(run())
    print(f"sent={sent}")


if __name__ == "__main__":
    main()
