from __future__ import annotations

import asyncio
import enum
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from urllib import error, request

from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)
DEFAULT_SMS_SENDER_NAME = "Soul Cuts"


class SmsDeliveryStatus(str, enum.Enum):
    enroute = "ENROUTE"
    delivered = "DELIVRD"
    expired = "EXPIRED"
    undeliverable = "UNDELIV"
    rejected = "REJECTD"


@dataclass(frozen=True)
class SmsSendResult:
    provider_message_id: str | None
    raw_response: dict


class SmsService:
    @staticmethod
    def validate_message_body(body: str) -> None:
        unsupported_codepoints = sorted(
            {
                ord(character)
                for character in body
                if ord(character) > 0xFFFF
                or 0xD800 <= ord(character) <= 0xDFFF
            }
        )
        if unsupported_codepoints:
            formatted_codepoints = ", ".join(
                f"U+{codepoint:04X}" for codepoint in unsupported_codepoints
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "SMS Club does not reliably support characters outside "
                    f"the Unicode BMP ({formatted_codepoints}). Remove these emoji."
                ),
            )

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
        sensitive: bool = False,
    ) -> SmsSendResult:
        self.validate_message_body(body)
        if settings.sms_provider == "stub":
            if sensitive:
                logger.info("Stub sensitive SMS accepted")
            else:
                logger.info("Stub SMS sent", extra={"phone": phone, "body": body, **(log_context or {})})
            return SmsSendResult(provider_message_id=None, raw_response={"provider": "stub"})

        if settings.sms_provider == "smsclub":
            return await self._send_smsclub_message(phone, body, lifetime_minutes=lifetime_minutes)

        raise NotImplementedError(f"Unsupported SMS provider: {settings.sms_provider}")

    async def _send_smsclub_otp(self, phone: str, code: str) -> None:
        await self._send_smsclub_message(
            phone,
            settings.sms_otp_template.format(code=code),
            lifetime_minutes=settings.otp_code_ttl_minutes,
        )

    async def _send_smsclub_message(
        self,
        phone: str,
        body: str,
        *,
        lifetime_minutes: int | None = None,
    ) -> SmsSendResult:
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
        provider_message_id = self._validate_smsclub_response(response_data, phone)
        return SmsSendResult(provider_message_id=provider_message_id, raw_response=response_data)

    async def get_message_statuses(
        self,
        provider_message_ids: Sequence[str],
    ) -> dict[str, SmsDeliveryStatus]:
        if not provider_message_ids:
            return {}
        if len(provider_message_ids) > 100:
            raise ValueError("SMS Club accepts at most 100 message IDs per status request")
        if settings.sms_provider != "smsclub":
            return {}
        if not settings.sms_club_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="SMS Club token is not configured",
            )

        message_ids = [str(message_id) for message_id in provider_message_ids]
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.sms_club_token}",
        }
        response_data = await asyncio.to_thread(
            self._post_json,
            f"{settings.sms_club_base_url}/sms/status",
            {"id_sms": message_ids},
            headers,
        )
        return self._validate_smsclub_status_response(response_data)

    def _post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        req = request.Request(
            url=url,
            # SMS Club requires UTF-8 input. ``validate_message_body`` keeps
            # supplementary-plane emoji out because the downstream SMS path
            # replaces them with question marks even when JSON is valid UTF-8.
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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

    def _validate_smsclub_response(self, response_data: dict, phone: str) -> str:
        success_request = response_data.get("success_request")
        if not isinstance(success_request, dict):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unexpected SMS provider response",
            )

        info = success_request.get("info")
        add_info = success_request.get("add_info")
        target_phone = self._smsclub_phone(phone)

        if isinstance(info, dict):
            for message_id, number in info.items():
                if str(number) == target_phone:
                    return str(message_id)

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

    def _validate_smsclub_status_response(self, response_data: dict) -> dict[str, SmsDeliveryStatus]:
        success_request = response_data.get("success_request")
        info = success_request.get("info") if isinstance(success_request, dict) else None
        if not isinstance(info, dict):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unexpected SMS provider status response",
            )

        statuses: dict[str, SmsDeliveryStatus] = {}
        for message_id, raw_status in info.items():
            try:
                statuses[str(message_id)] = SmsDeliveryStatus(str(raw_status).upper())
            except ValueError:
                logger.warning(
                    "SMS Club returned an unknown delivery status",
                    extra={"provider_message_id": str(message_id), "provider_status": str(raw_status)},
                )
        return statuses

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
