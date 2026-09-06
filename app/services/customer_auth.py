from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_scoped_access_token,
    generate_otp_code,
    hash_otp_code,
    verify_otp_code,
)
from app.models.customer import Customer
from app.models.customer_otp_code import CustomerOtpCode
from app.models.booking import Booking
from app.models.order import Order
from app.models.messaging import MessageRecipient
from app.services.sms import SmsService
from app.services.sms_queue import SmsQueuePending, use_sms_context

logger = logging.getLogger(__name__)

PHONE_ALLOWED_CHARS: Final[set[str]] = set("+0123456789() -")


@dataclass
class OtpRequestResult:
    expires_in_seconds: int
    retry_after_seconds: int
    sends_left_today: int
    debug_otp_code: str | None


@dataclass
class OtpVerifyResult:
    customer: Customer
    access_token: str
    is_new_customer: bool
    attempts_left_today: int


class CustomerAuthService:
    def __init__(self) -> None:
        self.sms_service = SmsService()

    def normalize_phone(self, phone: str) -> str:
        normalized = "".join(char for char in phone.strip() if char in PHONE_ALLOWED_CHARS)
        normalized = normalized.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not normalized.startswith("+"):
            normalized = f"+{normalized}"
        digits = normalized.removeprefix("+")
        if not digits.isdigit() or len(digits) < 10 or len(digits) > 15:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid phone number")
        return normalized

    async def request_otp(self, session: AsyncSession, phone: str) -> OtpRequestResult:
        normalized_phone = self.normalize_phone(phone)
        now = datetime.now(UTC)
        day_start, day_end = self._day_bounds(now)

        sent_today = await self._sent_count_today(session, normalized_phone, day_start, day_end)
        if sent_today >= settings.otp_max_sends_per_day:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Daily OTP send limit reached for this phone number",
            )

        latest_code = await self._latest_otp(session, normalized_phone)
        if latest_code is not None:
            cooldown_end = latest_code.sent_at + timedelta(seconds=settings.otp_resend_interval_seconds)
            if cooldown_end > now:
                retry_after = int((cooldown_end - now).total_seconds())
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"OTP already sent recently. Retry in {retry_after} seconds",
                )

        code = generate_otp_code()
        otp_record = CustomerOtpCode(
            phone=normalized_phone,
            code_hash=hash_otp_code(normalized_phone, code),
            sent_at=now,
            expires_at=now + timedelta(minutes=settings.otp_code_ttl_minutes),
        )
        session.add(otp_record)
        await session.commit()

        # A caller timeout must not replace durable OTP work or reset priority.
        try:
            with use_sms_context(priority=0, idempotency_key=f"otp:{otp_record.id}"):
                await self.sms_service.send_otp_code(normalized_phone, code)
        except SmsQueuePending:
            logger.info("OTP remains queued", extra={"otp_record_id": otp_record.id})

        return OtpRequestResult(
            expires_in_seconds=settings.otp_code_ttl_minutes * 60,
            retry_after_seconds=settings.otp_resend_interval_seconds,
            sends_left_today=max(settings.otp_max_sends_per_day - sent_today - 1, 0),
            debug_otp_code=code if settings.app_env in {"local", "development"} else None,
        )

    async def verify_otp(self, session: AsyncSession, phone: str, otp_code: str) -> OtpVerifyResult:
        normalized_phone = self.normalize_phone(phone)
        now = datetime.now(UTC)
        day_start, day_end = self._day_bounds(now)
        attempts_today = await self._attempts_count_today(session, normalized_phone, day_start, day_end)
        if attempts_today >= settings.otp_max_verify_attempts_per_day:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Daily OTP verification attempt limit reached for this phone number",
            )

        latest_code = await self._latest_otp(session, normalized_phone)
        if latest_code is None or latest_code.verified_at is not None or latest_code.expires_at < now:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP code is missing or expired")

        latest_code.attempts_count += 1
        latest_code.last_attempt_at = now
        attempts_left_today = max(settings.otp_max_verify_attempts_per_day - attempts_today - 1, 0)

        if not verify_otp_code(normalized_phone, otp_code, latest_code.code_hash):
            await session.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code")

        latest_code.verified_at = now
        result = await session.execute(select(Customer).where(Customer.phone == normalized_phone))
        customer = result.scalar_one_or_none()
        is_new_customer = False

        if customer is None:
            customer = Customer(phone=normalized_phone, is_active=True, phone_verified_at=now, last_login_at=now)
            session.add(customer)
            await session.flush()
            is_new_customer = True
        else:
            customer.phone_verified_at = now
            customer.last_login_at = now

        await session.commit()
        await session.refresh(customer)

        return OtpVerifyResult(
            customer=customer,
            access_token=self.issue_access_token(customer),
            is_new_customer=is_new_customer,
            attempts_left_today=attempts_left_today,
        )

    async def update_customer(
        self,
        session: AsyncSession,
        customer: Customer,
        data: dict,
    ) -> Customer:
        phone = data.get("phone")
        if phone:
            data["phone"] = self.normalize_phone(phone)

        await self._validate_unique_fields(session, data, exclude_customer_id=customer.id)

        for key, value in data.items():
            setattr(customer, key, value)

        await session.commit()
        await session.refresh(customer)
        return customer

    async def create_customer(self, session: AsyncSession, data: dict) -> Customer:
        data["phone"] = self.normalize_phone(data["phone"])
        await self._validate_unique_fields(session, data)

        customer = Customer(**data)
        session.add(customer)
        await session.commit()
        await session.refresh(customer)
        return customer

    async def delete_customer(self, session: AsyncSession, customer: Customer) -> None:
        # Serialize history inspection with new FK references from snapshots.
        # Otherwise a concurrent recipient insert could be erased by CASCADE.
        customer = (await session.execute(
            select(Customer).where(Customer.id == customer.id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one()
        order_count = (
            await session.execute(select(func.count()).select_from(Order).where(Order.customer_id == customer.id))
        ).scalar_one()
        booking_count = (
            await session.execute(select(func.count()).select_from(Booking).where(Booking.customer_id == customer.id))
        ).scalar_one()
        has_campaign_history = await session.scalar(
            select(MessageRecipient.id).where(
                MessageRecipient.customer_id == customer.id,
                MessageRecipient.run_id.is_not(None),
            ).limit(1)
        )
        if order_count or booking_count or has_campaign_history is not None:
            customer.is_active = False
            await session.commit()
            return

        await session.delete(customer)
        await session.commit()

    async def _validate_unique_fields(
        self,
        session: AsyncSession,
        data: dict,
        *,
        exclude_customer_id: int | None = None,
    ) -> None:
        email = data.get("email")
        if email:
            stmt = select(Customer).where(Customer.email == email)
            if exclude_customer_id is not None:
                stmt = stmt.where(Customer.id != exclude_customer_id)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already in use")
        phone = data.get("phone")
        if phone:
            stmt = select(Customer).where(Customer.phone == phone)
            if exclude_customer_id is not None:
                stmt = stmt.where(Customer.id != exclude_customer_id)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phone is already in use")

    def issue_access_token(self, customer: Customer) -> str:
        return create_scoped_access_token(
            subject=customer.id,
            scope="customer",
            expires_delta=timedelta(days=settings.customer_access_token_expire_days),
        )

    async def _sent_count_today(
        self,
        session: AsyncSession,
        phone: str,
        day_start: datetime,
        day_end: datetime,
    ) -> int:
        stmt = select(func.count()).select_from(CustomerOtpCode).where(
            CustomerOtpCode.phone == phone,
            CustomerOtpCode.sent_at >= day_start,
            CustomerOtpCode.sent_at < day_end,
        )
        return int((await session.execute(stmt)).scalar_one())

    async def _attempts_count_today(
        self,
        session: AsyncSession,
        phone: str,
        day_start: datetime,
        day_end: datetime,
    ) -> int:
        stmt = select(func.coalesce(func.sum(CustomerOtpCode.attempts_count), 0)).where(
            CustomerOtpCode.phone == phone,
            CustomerOtpCode.sent_at >= day_start,
            CustomerOtpCode.sent_at < day_end,
        )
        return int((await session.execute(stmt)).scalar_one())

    async def _latest_otp(self, session: AsyncSession, phone: str) -> CustomerOtpCode | None:
        stmt = (
            select(CustomerOtpCode)
            .where(CustomerOtpCode.phone == phone)
            .order_by(CustomerOtpCode.sent_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    def _day_bounds(self, now: datetime) -> tuple[datetime, datetime]:
        day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        return day_start, day_end
