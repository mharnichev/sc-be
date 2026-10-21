from __future__ import annotations

import hashlib
import hmac
import math
from datetime import timedelta
from typing import Literal

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.shop_ingress_limits import ShopIngressAdmission

# Admission commits independently even while authentication holds a business
# connection. A separate bounded pool avoids circular waits under saturation.
ingress_engine = create_async_engine(settings.sqlalchemy_database_uri, pool_size=2,
                                     max_overflow=2, pool_timeout=3, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(ingress_engine, expire_on_commit=False)


IngressAction = Literal['otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart']
# Per-subject cooldowns do not include the source: moving to another IP cannot reset them.
SUBJECT_BUDGETS = {'feedback': (1, 30), 'review': (1, 15), 'profile': (1, 1), 'cart': (60, 60)}


def _enabled() -> bool:
    return settings.app_env in {'production', 'staging'} or settings.shared_ingress_limits_enabled


async def _admit(action: IngressAction, identity: str, limit: int, window: int, *, session_factory=None) -> None:
    """Commit admission independently of business rollback; serialize across workers."""
    if not _enabled():
        return
    try:
        if limit < 1 or window < 1 or not settings.secret_key:
            raise ValueError('Invalid ingress configuration')
        key = hmac.new(settings.secret_key.encode(), identity.encode(), hashlib.sha256).hexdigest()
        lock_key = int.from_bytes(bytes.fromhex(key)[:8], 'big', signed=True)
        factory = session_factory if session_factory is not None else AsyncSessionLocal
        retry_after = None
        async with factory() as session:
            async with session.begin():
                await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
                now = await session.scalar(select(func.clock_timestamp()))
                cutoff = now - timedelta(seconds=window)
                same_key = (ShopIngressAdmission.source_key == key, ShopIngressAdmission.action == action)
                await session.execute(delete(ShopIngressAdmission).where(*same_key, ShopIngressAdmission.admitted_at <= cutoff))
                count, oldest = (await session.execute(select(
                    func.count(), func.min(ShopIngressAdmission.admitted_at),
                ).where(*same_key))).one()
                if count >= limit:
                    retry_after = max(1, math.ceil((oldest + timedelta(seconds=window) - now).total_seconds()))
                else:
                    session.add(ShopIngressAdmission(source_key=key, action=action, admitted_at=now))
    except Exception:
        raise HTTPException(status_code=503, detail={
            'code': 'ingress_unavailable', 'message': 'Request admission is temporarily unavailable',
        }) from None
    if retry_after is not None:
        raise HTTPException(status_code=429, detail={
            'code': 'ingress_limited', 'message': 'Too many requests',
        }, headers={'Retry-After': str(retry_after)})


async def admit_shop_ingress(request: Request, action: IngressAction, *, session_factory=None) -> None:
    """Use the ASGI peer, never client-supplied forwarding/visitor headers.

    Only trusted proxy middleware may rewrite request.client. Admission survives
    worker restarts. Expired rows are removed when the same bucket is reused.
    """
    limits = {'otp_send': settings.otp_source_send_limit,
              'otp_verify': settings.otp_source_verify_limit,
              'checkout': settings.checkout_source_limit,
              'feedback': settings.feedback_source_limit,
              'review': settings.review_source_limit,
              'profile': settings.profile_source_limit,
              'search': settings.search_source_limit,
              'cart': settings.cart_source_limit}
    source = request.client.host if request.client and request.client.host else 'unknown'
    await _admit(action, f'shop-ingress:v1:{action}:{source}', limits[action],
                 settings.source_limit_window_seconds, session_factory=session_factory)


async def admit_shop_subject(action: IngressAction, subject: str, *, session_factory=None) -> None:
    limit, window = SUBJECT_BUDGETS[action]
    await _admit(action, f'shop-subject:v1:{action}:{subject}', limit, window, session_factory=session_factory)
