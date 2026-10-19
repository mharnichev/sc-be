from __future__ import annotations

import hashlib
import hmac
import math
from datetime import timedelta
from typing import Literal

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.shop_ingress_limits import ShopIngressAdmission

IngressAction = Literal["otp_send", "otp_verify", "checkout"]


async def admit_shop_ingress(request: Request, action: IngressAction, *, session_factory=None) -> None:
    """Commit admission before business work, across all workers sharing PostgreSQL.

    Proxy headers are intentionally ignored: deployments must preserve the actual
    peer, or accept a shared proxy bucket. Only this key's expired events are
    cleaned; inactive digests remain until reused (there is no global sweeper).
    """
    if settings.app_env not in {"production", "staging"} and not settings.shared_ingress_limits_enabled:
        return

    try:
        limits = {"otp_send": settings.otp_source_send_limit,
                  "otp_verify": settings.otp_source_verify_limit,
                  "checkout": settings.checkout_source_limit}
        limit, window = limits[action], settings.source_limit_window_seconds
        if limit < 1 or window < 1 or not settings.secret_key:
            raise ValueError("Invalid ingress configuration")
        source = request.client.host if request.client and request.client.host else "unknown"
        key = hmac.new(settings.secret_key.encode(),
                       f"shop-ingress:v1:{action}:{source}".encode(), hashlib.sha256).hexdigest()
        lock_key = int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)
        factory = session_factory if session_factory is not None else AsyncSessionLocal
        retry_after = None
        async with factory() as session:
            async with session.begin():
                await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
                # Read the database clock after waiting for the per-source lock.
                now = await session.scalar(select(func.clock_timestamp()))
                cutoff = now - timedelta(seconds=window)
                same_key = (ShopIngressAdmission.source_key == key, ShopIngressAdmission.action == action)
                await session.execute(delete(ShopIngressAdmission).where(
                    *same_key, ShopIngressAdmission.admitted_at <= cutoff,
                ))
                count, oldest = (await session.execute(select(
                    func.count(), func.min(ShopIngressAdmission.admitted_at),
                ).where(*same_key))).one()
                if count >= limit:
                    retry_after = max(1, math.ceil((oldest + timedelta(seconds=window) - now).total_seconds()))
                else:
                    session.add(ShopIngressAdmission(source_key=key, action=action, admitted_at=now))
        # Even rejection-time cleanup has committed; no caller rollback can undo admission.
    except Exception:
        # Never expose the database exception, connection details or source identity.
        raise HTTPException(status_code=503, detail={
            "code": "ingress_unavailable", "message": "Request admission is temporarily unavailable",
        }) from None

    if retry_after is not None:
        raise HTTPException(status_code=429, detail={
            "code": "ingress_limited", "message": "Too many requests from this source",
        }, headers={"Retry-After": str(retry_after)})
