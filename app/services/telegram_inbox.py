"""Serialize one chat across business-action commits and deduplicate successes."""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.models.messaging import TelegramWebhookEvent
from app.core.database import OperationalSession

_reply_context = ContextVar("telegram_reply_context", default=None)


async def commit_event_business_action(session, result=None):
    """Commit successful business changes and the inbox receipt atomically."""
    context = _reply_context.get()
    if context is not None and context[0] is session:
        row = await session.get(TelegramWebhookEvent, context[1])
        row.state = "completed"
        row.completed_at = datetime.now(UTC)
        if result is not None:
            row.result_json = {**(row.result_json or {}), **result}
    await session.commit()


async def send_event_reply(provider, *, operation, destination, payload):
    """Return None outside webhook handling; persist each logical reply inside it."""
    context = _reply_context.get()
    if context is None:
        return None
    session, update_id, position = context
    from app.services.messaging import ProviderSendResult
    from app.services.telegram_delivery import durable_telegram_send
    body = json.dumps({"operation": operation, "payload": payload}, sort_keys=True, ensure_ascii=False)
    position[0] += 1
    class ReplyTransport:
        async def send_message(self, *, destination, body):
            snapshot = json.loads(body)
            kwargs = snapshot["payload"]
            if snapshot["operation"] == "photo":
                if kwargs.get("photo_path") is not None:
                    kwargs["photo_path"] = Path(kwargs["photo_path"])
                return await provider.send_photo(destination=destination, **kwargs)
            if snapshot["operation"] == "callback":
                result = await provider.answer_callback_query(**kwargs)
                return ProviderSendResult(None, result)
            return await provider.send_message(destination=destination, **kwargs)
    return await durable_telegram_send(session, ReplyTransport(),
        key=f"telegram-inbox:{update_id}:reply:{position[0]}", destination=destination, body=body)


async def process_telegram_event(session, *, update_id, chat_id, action):
    if update_id is None or chat_id is None:
        return await action(session)
    # A dedicated connection pins the session-level advisory lock through the
    # handler's own commits. Transaction-level locks cannot protect this flow.
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        engine = bind
    elif isinstance(bind, AsyncConnection):
        engine = bind.engine
    else:
        raise RuntimeError("Telegram inbox requires an asynchronous database binding")
    async with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        lock_key = int.from_bytes(hashlib.sha256(f"telegram-inbox:{chat_id}".encode()).digest()[:8],
                                  "big", signed=True)
        acquired = False
        acquisition_started = False
        acquisition_confirmed = False
        try:
            if postgres:
                acquisition_started = True
                acquired = bool(await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}))
                acquisition_confirmed = True
                await connection.commit()
                if not acquired:
                    raise HTTPException(503, "Telegram chat action is already processing")
            async with AsyncSession(bind=connection, sync_session_class=OperationalSession, expire_on_commit=False) as owned:
                row = await owned.get(TelegramWebhookEvent, update_id)
                if row is not None and row.chat_id != chat_id:
                    raise HTTPException(400, "Telegram event identity changed")
                if row is not None and row.state == "completed":
                    return {"ok": True, "handled": True, "action": "duplicate_update"}
                if row is None:
                    row = TelegramWebhookEvent(update_id=update_id, chat_id=chat_id, state="processing", attempts=0)
                    owned.add(row)
                row.state = "processing"
                row.attempts += 1
                row.last_error = None
                await owned.commit()
                reply_token = _reply_context.set((owned, update_id, [0]))
                try:
                    result = await action(owned)
                except Exception as exc:
                    await owned.rollback()
                    row = await owned.get(TelegramWebhookEvent, update_id)
                    if row.state != "completed":
                        row.state = "failed"
                        row.last_error = type(exc).__name__
                        await owned.commit()
                    raise
                finally:
                    _reply_context.reset(reply_token)
                action_name = str(result.get("action", ""))
                failed = not result.get("handled") or action_name.endswith((
                    "_failed", "_not_found", "_needs_contact", "_without_master", "_missing_context"))
                row.state = "failed" if failed else "completed"
                row.result_json = {**(row.result_json or {}), **result}
                row.completed_at = None if failed else datetime.now(UTC)
                await owned.commit()
                return result
        finally:
            if postgres and acquisition_started:
                try:
                    if acquired:
                        await connection.rollback()
                        unlocked = await connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                        await connection.commit()
                        if not unlocked:
                            await connection.invalidate()
                    elif not acquisition_confirmed:
                        # If acquisition was interrupted its result may be lost.
                        # Do not return that possibly locked connection to pool.
                        await connection.invalidate()
                except BaseException:
                    await connection.invalidate()
                    raise
