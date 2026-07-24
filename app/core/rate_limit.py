from __future__ import annotations

import hashlib
import hmac
import threading
import time

from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.core.config import settings


class SlidingWindowRateLimiter:
    """Small process-local guard; deployment ingress should enforce a second shared limit."""

    def __init__(self) -> None:
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._checks = 0

    def check(self, key: str, *, limit: int, window_seconds: int = 60) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            self._checks += 1
            if self._checks % 512 == 0:
                for existing_key, existing_entries in list(self._entries.items()):
                    while existing_entries and existing_entries[0] <= cutoff:
                        existing_entries.popleft()
                    if not existing_entries:
                        self._entries.pop(existing_key, None)
            entries = self._entries[key]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests",
                    headers={"Retry-After": str(window_seconds)},
                )
            entries.append(now)


def privacy_safe_rate_key(request: Request, token: str, action: str) -> str:
    client_host = request.client.host if request.client else "unknown"
    material = f"{action}:{client_host}:{token}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), material, hashlib.sha256).hexdigest()


review_rate_limiter = SlidingWindowRateLimiter()
booking_funnel_rate_limiter = SlidingWindowRateLimiter()
