from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
from typing import Any, Literal
from urllib import error, parse, request

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.blog import BlogBrevoSyncJob, BlogSubscription, BlogSubscriptionStatus
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
BREVO_SYNC_BATCH_SIZE = 20
BREVO_SYNC_POLL_SECONDS = 30
BREVO_SYNC_RETRY_BASE_SECONDS = 30
BREVO_SYNC_RETRY_MAX_SECONDS = 3600


@dataclass(frozen=True)
class BrevoSyncOutcome:
    status: Literal["success", "failed", "disabled"]
    error: str | None = None


class BrevoContactSyncService:
    session_factory = AsyncSessionLocal

    def __init__(self, session_factory=None) -> None:
        self.session_factory = session_factory or AsyncSessionLocal

    def is_configured(self) -> bool:
        return bool(getattr(settings, "brevo_sync_enabled", False) and getattr(settings, "brevo_api_key", None))

    async def sync_subscribed_contact(self, subscription: BlogSubscription) -> None:
        if subscription.id is not None:
            await self._sync_committed_subscription(subscription.id)
            return
        await self._send_state(subscription.email, BlogSubscriptionStatus.subscribed)

    async def sync_unsubscribed_contact(self, subscription: BlogSubscription) -> None:
        if subscription.id is not None:
            await self._sync_committed_subscription(subscription.id)
            return
        await self._send_state(subscription.email, BlogSubscriptionStatus.unsubscribed)

    async def _send_state(self, email: str, status: BlogSubscriptionStatus) -> BrevoSyncOutcome:
        if not self.is_configured():
            logger.info("Brevo contact sync skipped: integration is disabled or API key is missing")
            return BrevoSyncOutcome("disabled")

        if status == BlogSubscriptionStatus.unsubscribed:
            payload: dict[str, Any] = {"emailBlacklisted": True}
            if settings.brevo_contact_list_id is not None:
                payload["unlinkListIds"] = [settings.brevo_contact_list_id]
            identifier = parse.quote(email, safe="")
            return await self._safe_request("PUT", f"/contacts/{identifier}", payload)

        payload: dict[str, Any] = {
            "email": email,
            "emailBlacklisted": False,
            "updateEnabled": True,
        }

        if settings.brevo_contact_list_id is not None:
            payload["listIds"] = [settings.brevo_contact_list_id]

        return await self._safe_request("POST", "/contacts", payload)

    @staticmethod
    async def _wait_for_task(task):
        """Cancellation cannot abandon a still-running HTTP thread or lock cleanup."""
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception:
                break
        if cancelled is not None:
            if not task.cancelled():
                task.exception()
            raise cancelled
        return task.result()

    async def _safe_request(self, method: str, path: str, payload: dict[str, Any]) -> BrevoSyncOutcome:
        if not self.is_configured():
            return BrevoSyncOutcome("disabled")
        try:
            task = asyncio.create_task(asyncio.to_thread(self._request_json, method, path, payload))
            await self._wait_for_task(task)
            return BrevoSyncOutcome("success")
        except Exception as exc:
            # Failure includes ambiguous remote outcomes; it never acknowledges intent.
            cause = exc.__cause__ or exc
            code = f"http_{cause.code}" if isinstance(cause, error.HTTPError) else type(cause).__name__
            logger.warning("Brevo contact sync failed", extra={"error_type": code, "brevo_method": method})
            return BrevoSyncOutcome("failed", code)

    async def _sync_committed_subscription(self, subscription_id: int) -> None:
        try:
            await self._sync_subscription(subscription_id, wait_for_lock=True)
        except Exception as exc:
            # The public transition is already committed; durable intent survives.
            logger.warning("Brevo sync coordination failed", extra={"error_type": type(exc).__name__})

    async def _sync_subscription(self, subscription_id: int, *, wait_for_lock: bool) -> bool:
        if not self.is_configured():
            return False
        engine = self.session_factory.kw["bind"]
        async with engine.connect() as connection:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                email = await session.scalar(select(BlogSubscription.email).where(BlogSubscription.id == subscription_id))
                await session.commit()
                if email is None:
                    return False
            key = int.from_bytes(hashlib.sha256(f"brevo-contact:{email.strip().lower()}".encode()).digest()[:8],
                                 "big", signed=True)
            locked = False
            acquisition_uncertain = False
            primary_error = None
            try:
                acquisition_uncertain = True
                if wait_for_lock:
                    await connection.execute(select(func.pg_advisory_lock(key)))
                    locked = True
                else:
                    locked = bool(await connection.scalar(select(func.pg_try_advisory_lock(key))))
                acquisition_uncertain = False
                await connection.commit()
                if not locked:
                    return False
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    snapshot = (await session.execute(select(
                        BlogBrevoSyncJob.desired_version, BlogBrevoSyncJob.synced_version,
                        BlogBrevoSyncJob.attempts, BlogBrevoSyncJob.next_attempt_at,
                        BlogSubscription.email, BlogSubscription.status,
                    ).join(BlogSubscription, BlogSubscription.id == BlogBrevoSyncJob.subscription_id)
                        .where(BlogBrevoSyncJob.subscription_id == subscription_id))).first()
                    now = datetime.now(UTC)
                    if (snapshot is None or snapshot.desired_version <= snapshot.synced_version
                            or snapshot.next_attempt_at > now):
                        await session.commit()
                        return False
                    delay = min(BREVO_SYNC_RETRY_MAX_SECONDS,
                                BREVO_SYNC_RETRY_BASE_SECONDS * 2 ** min(snapshot.attempts, 10))
                    claimed = await session.execute(update(BlogBrevoSyncJob).where(
                        BlogBrevoSyncJob.subscription_id == subscription_id,
                        BlogBrevoSyncJob.desired_version == snapshot.desired_version,
                    ).values(attempt_version=snapshot.desired_version, attempts=BlogBrevoSyncJob.attempts + 1,
                             last_attempt_at=now, next_attempt_at=now + timedelta(seconds=delay),
                             last_error="attempt_in_progress", updated_at=now))
                    await session.commit()
                    if not claimed.rowcount:
                        return False
                    # Session-level advisory ownership survives this commit; no
                    # subscription/outbox row lock or transaction spans HTTP I/O.
                    outcome = await self._send_state(snapshot.email, snapshot.status)
                    now = datetime.now(UTC)
                    values = ({"synced_version": snapshot.desired_version, "last_error": None,
                               "next_attempt_at": now, "updated_at": now}
                              if outcome.status == "success" else
                              {"last_error": outcome.error or outcome.status, "updated_at": now})
                    await session.execute(update(BlogBrevoSyncJob).where(
                        BlogBrevoSyncJob.subscription_id == subscription_id,
                        BlogBrevoSyncJob.desired_version == snapshot.desired_version,
                    ).values(**values))
                    await session.commit()
                    return True
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if locked or acquisition_uncertain:
                    async def release_lock():
                        try:
                            if acquisition_uncertain:
                                # Cancellation/disconnect may occur after PostgreSQL
                                # granted a session lock but before the reply arrives.
                                await connection.invalidate()
                                return
                            if connection.in_transaction():
                                await connection.rollback()
                            released = await connection.scalar(select(func.pg_advisory_unlock(key)))
                            if not released:
                                raise RuntimeError("Brevo advisory ownership was lost")
                            await connection.commit()
                        except BaseException:
                            await connection.invalidate()
                            raise

                    try:
                        await self._wait_for_task(asyncio.create_task(release_lock()))
                    except BaseException as exc:
                        if primary_error is None:
                            raise
                        logger.warning("Brevo lock cleanup failed", extra={"error_type": type(exc).__name__})

    async def sync_pending(self, limit: int = BREVO_SYNC_BATCH_SIZE) -> int:
        if not self.is_configured() or limit <= 0:
            return 0
        async with self.session_factory() as session:
            ids = list(await session.scalars(select(BlogBrevoSyncJob.subscription_id).where(
                BlogBrevoSyncJob.desired_version > BlogBrevoSyncJob.synced_version,
                BlogBrevoSyncJob.next_attempt_at <= datetime.now(UTC),
            ).order_by(BlogBrevoSyncJob.next_attempt_at, BlogBrevoSyncJob.subscription_id)
                .limit(min(limit, BREVO_SYNC_BATCH_SIZE))))
        processed = 0
        for subscription_id in ids:
            try:
                processed += bool(await self._sync_subscription(subscription_id, wait_for_lock=False))
            except Exception as exc:
                logger.warning("Brevo queued sync failed", extra={"error_type": type(exc).__name__})
        return processed

    def _request_json(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not settings.brevo_api_key:
            raise RuntimeError("BREVO_API_KEY is required for Brevo contact sync")

        url = f"{settings.brevo_api_base_url.rstrip('/')}/{path.lstrip('/')}"
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "api-key": settings.brevo_api_key,
            },
            method=method,
        )

        try:
            with request.urlopen(req, timeout=settings.brevo_timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Brevo API failed with status {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError("Brevo API is unavailable") from exc

        if not raw_body:
            return {}

        return json.loads(raw_body)


brevo_contact_sync_service = BrevoContactSyncService()
