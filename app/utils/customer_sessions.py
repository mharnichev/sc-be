from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from app.core.config import settings
from app.core.security import ALGORITHM, decode_token


def session_version(customer) -> int:
    return int(getattr(customer, "session_version", 0) or 0)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_customer_token(customer) -> str:
    return jwt.encode({
        "sub": str(customer.id), "scope": "customer",
        "exp": datetime.now(UTC) + timedelta(days=settings.customer_access_token_expire_days),
        "customer_session_version": session_version(customer),
        "jti": secrets.token_urlsafe(24),
    }, settings.secret_key, algorithm=ALGORITHM)


def customer_token_allowed(customer, token: str) -> bool:
    try:
        payload = decode_token(token)
    except (JWTError, ValueError, TypeError):
        return False
    if payload.get("scope") != "customer" or payload.get("sub") != str(customer.id):
        return False
    version = payload.get("customer_session_version", 0)
    if type(version) is not int:
        return False
    if version == session_version(customer):
        return True
    retained = getattr(customer, "retained_access_token_hash", None)
    return isinstance(retained, str) and hmac.compare_digest(retained, token_hash(token))
