"""Durable ownership of Telegram sends, including uncertain provider outcomes."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.messaging import TelegramDeliveryAttempt


class TelegramRejected(RuntimeError):
    """A structured Telegram response definitively rejected the request."""


class TelegramDestinationUnavailable(TelegramRejected):
    """The provider definitively rejected this destination."""


class TelegramDeliveryUncertain(RuntimeError):
    """Delivery may have been accepted; automatic resend is prohibited."""


class TelegramDeliveryConflict(ValueError):
    """The same logical delivery key was reused for a different message."""


async def durable_telegram_send(session, provider, *, key, destination, body, reply_markup=None,
                                enforce_payload_identity=False):
    # Lock an existing row or insert a unique ownership row. A competing insert
    # waits for the winner's commit, then observes its durable attempting state.
    row = (await session.execute(select(TelegramDeliveryAttempt)
        .where(TelegramDeliveryAttempt.key == key).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        row = TelegramDeliveryAttempt(key=key, state="attempting", destination=destination,
            body=body, attempts=1, started_at=datetime.now(UTC))
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = (await session.execute(select(TelegramDeliveryAttempt)
                .where(TelegramDeliveryAttempt.key == key).with_for_update()
                .execution_options(populate_existing=True))).scalar_one()
        else:
            await session.commit()
            return await _send_owned(session, provider, row, reply_markup)
    if enforce_payload_identity and (row.destination != destination or row.body != body):
        await session.commit()
        raise TelegramDeliveryConflict("Telegram delivery key belongs to a different message")
    if row.state == "accepted":
        from app.services.messaging import ProviderSendResult
        result = ProviderSendResult(row.provider_message_id, {"ok": True, "deduplicated": True})
        await session.commit()
        return result
    if row.state != "rejected":
        await session.commit()
        raise TelegramDeliveryUncertain("Telegram delivery is awaiting outcome reconciliation")
    row.state = "attempting"
    row.attempts += 1
    row.started_at = datetime.now(UTC)
    row.completed_at = None
    row.last_error = None
    # Freeze the exact original logical message, even if a caller rerenders it.
    await session.commit()
    return await _send_owned(session, provider, row, reply_markup)


async def _send_owned(session, provider, row, reply_markup):
    try:
        kwargs = {"destination": row.destination, "body": row.body}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        result = await provider.send_message(**kwargs)
    except TelegramRejected as exc:
        row.state = "rejected"
        row.last_error = str(exc)
        row.completed_at = datetime.now(UTC)
        await session.commit()
        raise
    except Exception as exc:
        row.state = "ambiguous"
        row.last_error = "Telegram provider outcome is uncertain"
        row.completed_at = datetime.now(UTC)
        await session.commit()
        raise TelegramDeliveryUncertain(row.last_error) from exc
    # Cancellation/process death deliberately leaves the already committed
    # attempting row. Recovery must treat that state as uncertain too.
    row.state = "accepted"
    row.provider_message_id = result.provider_message_id
    row.completed_at = datetime.now(UTC)
    await session.commit()
    return result
