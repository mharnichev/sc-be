from __future__ import annotations

import asyncio
import json
import logging
from urllib import error, request

from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)
DEFAULT_SMS_SENDER_NAME = "Soul Cuts"


class SmsService:
    async def send_otp_code(self, phone: str, code: str) -> None:
        await self.send_message(
            phone,
            settings.sms_otp_template.format(code=code),
            lifetime_minutes=settings.otp_code_ttl_minutes,
            log_context={"otp_code": code},
        )

    async def send_message(
        self,
        phone: str,
        body: str,
        *,
        lifetime_minutes: int | None = None,
        log_context: dict | None = None,
    ) -> None:
        if settings.sms_provider == "stub":
            logger.info("Stub SMS sent", extra={"phone": phone, "body": body, **(log_context or {})})
            return

        if settings.sms_provider == "smsclub":
            await self._send_smsclub_message(phone, body, lifetime_minutes=lifetime_minutes)
            return

        raise NotImplementedError(f"Unsupported SMS provider: {settings.sms_provider}")

    async def _send_smsclub_otp(self, phone: str, code: str) -> None:
        await self._send_smsclub_message(
            phone,
            settings.sms_otp_template.format(code=code),
            lifetime_minutes=settings.otp_code_ttl_minutes,
        )

    async def _send_smsclub_message(self, phone: str, body: str, *, lifetime_minutes: int | None = None) -> None:
        if not settings.sms_club_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="SMS Club token is not configured",
            )
        payload = {
            "phone": [self._smsclub_phone(phone)],
            "message": body,
        }
        sender_name = settings.sms_sender_name or DEFAULT_SMS_SENDER_NAME
        if sender_name:
            payload["src_addr"] = sender_name
        if lifetime_minutes is not None:
            payload["lifetime"] = lifetime_minutes
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.sms_club_token}",
        }
        response_data = await asyncio.to_thread(self._post_json, f"{settings.sms_club_base_url}/sms/send", payload, headers)
        self._validate_smsclub_response(response_data, phone)

    def _post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            detail = self._extract_provider_detail(body) or f"SMS provider request failed with status {exc.code}"
            if exc.code in {429, 453}:
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail) from exc
            if exc.code == 401:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SMS provider authentication failed") from exc
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
        except error.URLError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="SMS provider is unavailable",
            ) from exc

    def _validate_smsclub_response(self, response_data: dict, phone: str) -> None:
        success_request = response_data.get("success_request")
        if not isinstance(success_request, dict):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unexpected SMS provider response",
            )

        info = success_request.get("info")
        add_info = success_request.get("add_info")
        target_phone = self._smsclub_phone(phone)

        if isinstance(info, dict) and any(number == target_phone for number in info.values()):
            return

        if isinstance(add_info, dict):
            phone_error = add_info.get(target_phone)
            src_addr_error = add_info.get("src_addr")
            if phone_error:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=phone_error)
            if src_addr_error:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=src_addr_error)
            first_error = next(iter(add_info.values()), None)
            if isinstance(first_error, str):
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=first_error)

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SMS provider did not confirm message delivery to queue",
        )

    def _extract_provider_detail(self, response_body: str) -> str | None:
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError:
            return None

        success_request = payload.get("success_request")
        if not isinstance(success_request, dict):
            return None
        add_info = success_request.get("add_info")
        if isinstance(add_info, dict):
            first_error = next(iter(add_info.values()), None)
            if isinstance(first_error, str):
                return first_error
        return None

    def _smsclub_phone(self, phone: str) -> str:
        return "".join(char for char in phone if char.isdigit())
