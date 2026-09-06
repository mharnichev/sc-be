from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib import error, request

from fastapi import HTTPException, status

from app.core.config import settings
from app.services.sms_queue import (
    SmsQueueService, SmsRequestContext, SmsTransportError, sms_request_context, use_sms_context,
)

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
    def __init__(self, queue: SmsQueueService | None = None) -> None:
        self._queue = queue

    def _get_queue(self) -> SmsQueueService:
        if getattr(self, "_queue", None) is None:
            self._queue = SmsQueueService(transport=self._execute_queue_job)
        return self._queue

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
        current = sms_request_context.get() or SmsRequestContext()
        with use_sms_context(replace(current, priority=0, enqueue_only=False)):
            await self.send_message(
                phone, settings.sms_otp_template.format(code=code),
                lifetime_minutes=settings.otp_code_ttl_minutes,
                log_context={"purpose": "otp"}, sensitive=True,
            )

    async def send_message(
        self,
        phone: str,
        body: str,
        *,
        lifetime_minutes: int | None = None,
        log_context: dict | None = None,
        sensitive: bool = False,
        queue_session=None,
    ) -> SmsSendResult:
        self.validate_message_body(body)
        if settings.sms_provider == "stub":
            if sensitive:
                logger.info("Stub sensitive SMS accepted")
            else:
                logger.info("Stub SMS sent", extra={"phone": phone, "body": body, **(log_context or {})})
            return SmsSendResult(provider_message_id=None, raw_response={"provider": "stub"})

        if settings.sms_provider == "smsclub":
            return await self._send_smsclub_message(phone, body, lifetime_minutes=lifetime_minutes,
                                                   queue_session=queue_session)

        raise NotImplementedError(f"Unsupported SMS provider: {settings.sms_provider}")

    async def _send_smsclub_otp(self, phone: str, code: str) -> None:
        await self.send_otp_code(phone, code)

    async def _send_smsclub_message(
        self,
        phone: str,
        body: str,
        *,
        lifetime_minutes: int | None = None,
        queue_session=None,
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
        context = sms_request_context.get() or SmsRequestContext()
        options = {"priority": context.priority, "context": context,
                   "idempotency_key": context.idempotency_key}
        if lifetime_minutes is not None:
            options["expires_at"] = datetime.now(UTC) + timedelta(minutes=lifetime_minutes)
        if context.enqueue_only:
            if queue_session is not None:
                options["external_session"] = queue_session
            job = await self._get_queue().enqueue("send", payload, **options)
            return SmsSendResult(None, {"queued": True, "job_id": job.id})
        if queue_session is not None:
            raise ValueError("A caller transaction is supported only for enqueue-only SMS")
        response_data = await self._get_queue().request("send", payload, **options)
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
        response_data = await self._get_queue().request("status", {"id_sms": message_ids}, priority=200)
        return self._validate_smsclub_status_response(response_data)

    async def _execute_queue_job(self, job) -> dict:
        """Only the durable queue invokes this transport boundary."""
        if not settings.sms_club_token:
            raise SmsTransportError(503, "SMS Club token is not configured", code="authentication")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.sms_club_token}"}
        response_data = {}
        try:
            response_data = await asyncio.to_thread(
                self._post_json, f"{settings.sms_club_base_url.rstrip('/')}/sms/{job.operation}", job.payload, headers,
            )
            if not isinstance(response_data, dict):
                raise SmsTransportError(503, "Unexpected SMS provider response", code="malformed_response",
                                        ambiguous=job.operation == "send", retryable=job.operation == "status")
            if job.operation == "send":
                message_id = self._validate_smsclub_response(response_data, job.payload["phone"][0])
                return {"success_request": {"info": {message_id: self._smsclub_phone(job.payload["phone"][0])}}}
            else:
                statuses = self._validate_smsclub_status_response(response_data)
                return {"success_request": {"info": {key: value.value for key, value in statuses.items()}}}
        except SmsTransportError:
            raise
        except HTTPException as exc:
            success = response_data.get("success_request")
            add_info = success.get("add_info") if isinstance(success, dict) else None
            definitive = isinstance(add_info, dict) and bool(add_info)
            raise SmsTransportError(exc.status_code, "SMSClub rejected the recipient or sender" if definitive else "SMSClub returned no confirmed message ID",
                                    code="provider_rejected" if definitive else "missing_message_id",
                                    ambiguous=job.operation == "send" and not definitive,
                                    retryable=job.operation == "status") from exc

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
            exc.read()
            if exc.code == 429:
                raise SmsTransportError(429, "SMSClub request limit reached", code="rate_limited", retryable=True,
                                        retry_after_seconds=self._retry_after((exc.headers or {}).get("Retry-After"))) from exc
            if exc.code == 453:
                raise SmsTransportError(400, "SMSClub suppressed a duplicate message", code="duplicate_suppressed") from exc
            if exc.code == 401:
                raise SmsTransportError(503, "SMS provider authentication failed", code="authentication") from exc
            if exc.code >= 500:
                raise SmsTransportError(503, "SMS provider server failure", code="provider_server_error",
                                        retryable=True, ambiguous=True,
                                        retry_after_seconds=self._retry_after((exc.headers or {}).get("Retry-After"))) from exc
            raise SmsTransportError(400, "SMS provider rejected the request", code="provider_rejected") from exc
        except error.URLError as exc:
            raise SmsTransportError(503, "SMS provider connection failed", code="network_uncertain",
                                    retryable=True, ambiguous=True) from exc
        except (TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmsTransportError(503, "SMS provider returned no usable response", code="transport_uncertain",
                                    retryable=True, ambiguous=True) from exc

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
            return max(0, seconds) if math.isfinite(seconds) and seconds <= 31536000 else None
        except ValueError:
            try:
                at = parsedate_to_datetime(value)
                if at.utcoffset() is None:
                    at = at.replace(tzinfo=UTC)
                return max(0, (at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

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
