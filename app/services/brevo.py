from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib import error, parse, request

from app.core.config import settings
from app.models.blog import BlogSubscription

logger = logging.getLogger(__name__)


class BrevoContactSyncService:
    def is_configured(self) -> bool:
        return bool(settings.brevo_sync_enabled and settings.brevo_api_key)

    async def sync_subscribed_contact(self, subscription: BlogSubscription) -> None:
        if not self.is_configured():
            logger.info("Brevo contact sync skipped: integration is disabled or API key is missing")
            return

        payload: dict[str, Any] = {
            "email": subscription.email,
            "emailBlacklisted": False,
            "updateEnabled": True,
        }

        if settings.brevo_contact_list_id is not None:
            payload["listIds"] = [settings.brevo_contact_list_id]

        await self._safe_request("POST", "/contacts", payload)

    async def sync_unsubscribed_contact(self, subscription: BlogSubscription) -> None:
        if not self.is_configured():
            logger.info("Brevo contact sync skipped: integration is disabled or API key is missing")
            return

        payload: dict[str, Any] = {
            "emailBlacklisted": True,
        }

        if settings.brevo_contact_list_id is not None:
            payload["unlinkListIds"] = [settings.brevo_contact_list_id]

        identifier = parse.quote(subscription.email, safe="")
        await self._safe_request("PUT", f"/contacts/{identifier}", payload)

    async def _safe_request(self, method: str, path: str, payload: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._request_json, method, path, payload)
        except Exception:
            logger.exception("Brevo contact sync failed", extra={"brevo_path": path, "brevo_method": method})

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
