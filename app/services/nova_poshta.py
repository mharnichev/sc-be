from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.shop import DeliveryCache


class NovaPoshtaService:
    cache_ttl = timedelta(hours=24)

    async def cities(self, session: AsyncSession, query: str) -> tuple[list[dict[str, Any]], bool, datetime | None]:
        cache_key = f"np:cities:{query.strip().lower()}"
        return await self._cached_call(
            session,
            cache_key=cache_key,
            model_name="Address",
            called_method="getCities",
            method_properties={"FindByString": query.strip(), "Limit": "50"},
        )

    async def warehouses(
        self,
        session: AsyncSession,
        city_ref: str,
    ) -> tuple[list[dict[str, Any]], bool, datetime | None]:
        cache_key = f"np:warehouses:{city_ref}"
        return await self._cached_call(
            session,
            cache_key=cache_key,
            model_name="AddressGeneral",
            called_method="getWarehouses",
            method_properties={"CityRef": city_ref, "Limit": "1000"},
        )

    async def warehouse_types(self, session: AsyncSession) -> tuple[list[dict[str, Any]], bool, datetime | None]:
        return await self._cached_call(
            session,
            cache_key="np:warehouse-types",
            model_name="AddressGeneral",
            called_method="getWarehouseTypes",
            method_properties={},
        )

    async def _cached_call(
        self,
        session: AsyncSession,
        *,
        cache_key: str,
        model_name: str,
        called_method: str,
        method_properties: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, datetime | None]:
        now = datetime.now(UTC)
        cache = (
            await session.execute(
                select(DeliveryCache).where(
                    DeliveryCache.cache_key == cache_key,
                    DeliveryCache.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if cache:
            return list(cache.payload_json.get("items", [])), True, cache.updated_at

        if not settings.nova_poshta_api_key:
            return [], False, None

        items = await self._call_api(
            model_name=model_name,
            called_method=called_method,
            method_properties=method_properties,
        )
        existing = (
            await session.execute(select(DeliveryCache).where(DeliveryCache.cache_key == cache_key))
        ).scalar_one_or_none()
        if existing:
            existing.payload_json = {"items": items}
            existing.expires_at = now + self.cache_ttl
        else:
            session.add(
                DeliveryCache(
                    cache_key=cache_key,
                    payload_json={"items": items},
                    expires_at=now + self.cache_ttl,
                )
            )
        await session.commit()
        return items, False, now

    async def _call_api(
        self,
        *,
        model_name: str,
        called_method: str,
        method_properties: dict[str, Any],
    ) -> list[dict[str, Any]]:
        payload = {
            "apiKey": settings.nova_poshta_api_key,
            "modelName": model_name,
            "calledMethod": called_method,
            "methodProperties": method_properties,
        }

        def request() -> dict[str, Any]:
            body = json.dumps(payload).encode("utf-8")
            http_request = urllib.request.Request(
                settings.nova_poshta_api_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(http_request, timeout=settings.nova_poshta_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            data = await asyncio.to_thread(request)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Nova Poshta service is unavailable",
            ) from exc

        if not data.get("success", False):
            errors = data.get("errors") or data.get("warnings") or ["Nova Poshta request failed"]
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=errors[0] if isinstance(errors, list) and errors else "Nova Poshta request failed",
            )
        return list(data.get("data", []))
