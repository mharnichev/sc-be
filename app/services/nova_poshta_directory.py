from __future__ import annotations

import asyncio
import logging
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any
from weakref import WeakValueDictionary

from fastapi import HTTPException
from sqlalchemy import case, delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.nova_poshta import NovaPoshtaBranch, NovaPoshtaSettlement
from app.models.shop import DeliveryCache
from app.services.nova_poshta import NovaPoshtaService

logger = logging.getLogger(__name__)
DIRECTORY_KEY = "np:directory:v1"
POPULAR = ("Київ", "Львів", "Одеса", "Дніпро", "Харків", "Запоріжжя", "Вінниця", "Івано-Франківськ")
_street_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).lower().replace("’", "'").replace("ʼ", "'").split())


def truth(value: Any) -> bool:
    return value is True or str(value).lower() in {"1", "true"}


class NovaPoshtaDirectory:
    def __init__(self) -> None:
        self.provider = NovaPoshtaService()

    async def _pages(self, method: str) -> list[dict]:
        """Use provider totals and reject truncated/repeated pages before publishing."""
        rows: dict[str, dict] = {}
        for page in range(1, 1001):
            for attempt in range(5):
                try:
                    response = await self.provider._request_api(
                        model_name="AddressGeneral" if method == "getWarehouses" else "Address",
                        called_method=method,
                        method_properties={"Page": str(page), "Limit": "500"},
                    )
                    break
                except HTTPException as exc:
                    limited = exc.status_code == 429 or "many requests" in str(exc.detail).lower()
                    if attempt == 4 or not (limited or exc.status_code == 503):
                        raise
                    await asyncio.sleep(60 if limited else 2 ** (attempt + 1))
            batch = response["data"]
            info = response.get("info") or {}
            total = info.get("totalCount") if isinstance(info, dict) else None
            if not batch:
                if total is not None and len(rows) < int(total):
                    raise ValueError(f"Incomplete Nova Poshta {method} directory")
                return list(rows.values())
            before = len(rows)
            for item in batch:
                ref = item.get("Ref")
                if not isinstance(ref, str) or not ref:
                    raise ValueError(f"Missing reference in Nova Poshta {method}")
                rows[ref] = item
            if len(rows) == before:
                raise ValueError(f"Repeated Nova Poshta {method} page")
            if total is not None and len(rows) >= int(total):
                return list(rows.values())
            await asyncio.sleep(1)
        raise ValueError("Nova Poshta pagination safety limit exceeded")

    async def refresh(self, session: AsyncSession, *, force: bool = False) -> bool:
        if not settings.nova_poshta_api_key:
            return False
        # A transaction-scoped lock also coordinates multiple Uvicorn workers.
        locked = await session.scalar(text("SELECT pg_try_advisory_xact_lock(731028410)"))
        if not locked:
            await session.rollback()
            return False
        try:
            state = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == DIRECTORY_KEY))
            if state and state.expires_at > datetime.now(UTC) and not force:
                await session.rollback()
                return False
            settlements = await self._pages("getSettlements")
            branches = await self._pages("getWarehouses")
            if not settlements or not branches:
                raise ValueError("Refusing to replace Nova Poshta directory with an empty snapshot")
            settlement_rows, branch_rows = self.prepare(settlements, branches)
            if not settlement_rows or not branch_rows:
                raise ValueError("Nova Poshta snapshot has no usable delivery points")
            await session.execute(delete(NovaPoshtaBranch))
            await session.execute(delete(NovaPoshtaSettlement))
            for model, rows in ((NovaPoshtaSettlement, settlement_rows), (NovaPoshtaBranch, branch_rows)):
                for offset in range(0, len(rows), 500):
                    await session.execute(insert(model), rows[offset:offset + 500])
            now = datetime.now(UTC)
            expires = now + timedelta(hours=settings.nova_poshta_directory_refresh_hours)
            counts = {"settlements": len(settlement_rows), "branches": len(branch_rows)}
            if state:
                state.payload_json = counts
                state.updated_at = now
                state.expires_at = expires
            else:
                session.add(DeliveryCache(cache_key=DIRECTORY_KEY, payload_json=counts, expires_at=expires, updated_at=now))
            await session.execute(delete(DeliveryCache).where(
                DeliveryCache.cache_key.like("np:street%"), DeliveryCache.updated_at < now - timedelta(days=7),
            ))
            await session.commit()
            logger.info("Nova Poshta directory refreshed", extra=counts)
            return True
        except BaseException:
            await session.rollback()
            raise

    @staticmethod
    def prepare(settlements: list[dict], branches: list[dict]) -> tuple[list[dict], list[dict]]:
        city_refs = {b["SettlementRef"]: b.get("CityRef") for b in branches if b.get("SettlementRef") and b.get("CityRef")}
        settlement_rows = []
        refs = set()
        for item in settlements:
            ref, name = item["Ref"], item.get("Description", "")
            if not name:
                raise ValueError("Nova Poshta settlement has no name")
            area, region = item.get("AreaDescription", ""), item.get("RegionsDescription", "")
            kind = item.get("SettlementTypeDescription", "")
            rank = POPULAR.index(name) if name in POPULAR and kind == "місто" else 100
            payload = {
                "ref": ref, "city_ref": city_refs.get(ref), "name": name,
                "label": ", ".join(x for x in (f"{kind} {name}".strip(), region, area) if x),
                "area": area, "region": region,
                "delivery_available": truth(item.get("AddressDeliveryAllowed")),
            }
            settlement_rows.append({
                "ref": ref, "city_ref": city_refs.get(ref), "name": name,
                "search_text": normalize(" ".join(str(item.get(k) or "") for k in (
                    "Description", "DescriptionRu", "DescriptionTranslit", "AreaDescription", "RegionsDescription",
                ))), "popular_rank": rank, "delivery_available": payload["delivery_available"], "payload": payload,
            })
            refs.add(ref)
        branch_rows = []
        for item in branches:
            settlement_ref = item.get("SettlementRef")
            if settlement_ref not in refs:
                # A branch outside the settlement directory is not safe to publish.
                raise ValueError("Nova Poshta branch references an unknown settlement")
            if item.get("WarehouseStatus") != "Working" or truth(item.get("DenyToSelect")):
                continue
            number = str(item.get("Number") or "")
            name = item.get("Description") or item.get("ShortAddress")
            if not number or not name:
                raise ValueError("Nova Poshta branch has no number or name")
            payload = {
                "ref": item["Ref"], "settlement_ref": settlement_ref, "city_ref": item.get("CityRef"),
                "number": number, "name": name, "label": name, "address": item.get("ShortAddress") or name,
                "type": "postomat" if item.get("CategoryOfWarehouse") == "Postomat" else "warehouse",
                "status": item["WarehouseStatus"],
            }
            branch_rows.append({
                "ref": item["Ref"], "settlement_ref": settlement_ref, "number": number,
                "sort_number": int(number) if number.isdigit() else 2147483647,
                "search_text": normalize(" ".join(str(item.get(k) or "") for k in ("Number", "Description", "DescriptionRu", "ShortAddress", "ShortAddressRu"))),
                "payload": payload,
            })
        return settlement_rows, branch_rows

    async def metadata(self, session: AsyncSession) -> dict:
        state = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == DIRECTORY_KEY))
        if state is None:
            raise HTTPException(status_code=503, detail="Nova Poshta directory is being prepared. Please retry shortly.")
        return {"cached": True, "updated_at": state.updated_at, "stale": state.expires_at <= datetime.now(UTC)}

    async def settlements(self, session: AsyncSession, q: str = "", limit: int = 20) -> dict:
        metadata = await self.metadata(session)
        query = normalize(q)
        stmt = select(NovaPoshtaSettlement)
        if query:
            stmt = stmt.where(NovaPoshtaSettlement.search_text.contains(query, autoescape=True))
        else:
            stmt = stmt.where(NovaPoshtaSettlement.popular_rank < 100)
        stmt = stmt.order_by(
            case((NovaPoshtaSettlement.name == q.strip(), 0), else_=1),
            NovaPoshtaSettlement.popular_rank, NovaPoshtaSettlement.name, NovaPoshtaSettlement.ref,
        ).limit(limit)
        rows = (await session.scalars(stmt)).all()
        return {"items": [row.payload for row in rows], **metadata}

    async def branches(self, session: AsyncSession, settlement_ref: str, q: str = "", limit: int = 30) -> dict:
        metadata = await self.metadata(session)
        if await session.get(NovaPoshtaSettlement, settlement_ref) is None:
            raise HTTPException(status_code=404, detail="Settlement not found")
        stmt = select(NovaPoshtaBranch).where(NovaPoshtaBranch.settlement_ref == settlement_ref)
        query = normalize(q.strip().removeprefix("№"))
        if query:
            stmt = stmt.where(NovaPoshtaBranch.search_text.contains(query, autoescape=True))
        stmt = stmt.order_by(
            case((NovaPoshtaBranch.number == query, 0), else_=1), NovaPoshtaBranch.sort_number, NovaPoshtaBranch.ref,
        ).limit(limit)
        rows = (await session.scalars(stmt)).all()
        return {"items": [row.payload for row in rows], **metadata}

    async def streets(self, session: AsyncSession, settlement_ref: str, q: str, limit: int = 20) -> dict:
        settlement = await session.get(NovaPoshtaSettlement, settlement_ref)
        if settlement is None:
            raise HTTPException(status_code=404, detail="Settlement not found")
        if not settlement.delivery_available:
            raise HTTPException(status_code=422, detail="Address delivery is unavailable in this settlement")
        key = f"np:streets:{settlement_ref}:{normalize(q)}:{limit}"
        lock = _street_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Serialize cache misses across API processes too, then re-read the cache.
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
            cache = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == key))
            now = datetime.now(UTC)
            if cache and cache.expires_at > now:
                return {"items": cache.payload_json["items"], "cached": True, "updated_at": cache.updated_at, "stale": False}
            try:
                if not settings.nova_poshta_api_key:
                    raise HTTPException(status_code=503, detail="Nova Poshta street search is unavailable")
                raw = await self.provider._call_api(model_name="Address", called_method="searchSettlementStreets", method_properties={
                    "SettlementRef": settlement_ref, "StreetName": q.strip(), "Limit": str(limit),
                })
                addresses = raw[0].get("Addresses") if raw else []
                if not isinstance(addresses, list):
                    raise HTTPException(status_code=502, detail="Invalid Nova Poshta street response")
                items = []
                for item in addresses:
                    if item.get("SettlementRef") != settlement_ref or not item.get("SettlementStreetRef"):
                        raise HTTPException(status_code=502, detail="Invalid Nova Poshta street reference")
                    items.append({"ref": item["SettlementStreetRef"], "name": item["SettlementStreetDescription"], "label": item["Present"]})
            except HTTPException:
                if cache and cache.updated_at >= now - timedelta(days=7):
                    return {"items": cache.payload_json["items"], "cached": True, "updated_at": cache.updated_at, "stale": True}
                raise
            # Upsert both search cache and per-street provenance for order validation.
            values = [{"cache_key": key, "payload_json": {"items": items}, "expires_at": now + timedelta(hours=24), "updated_at": now}]
            values.extend({"cache_key": f"np:street:{item['ref']}", "payload_json": {**item, "settlement_ref": settlement_ref}, "expires_at": now + timedelta(days=7), "updated_at": now} for item in items)
            for value in values:
                stmt = pg_insert(DeliveryCache).values(**value)
                await session.execute(stmt.on_conflict_do_update(index_elements=[DeliveryCache.cache_key], set_={k: v for k, v in value.items() if k != "cache_key"}))
            await session.commit()
            return {"items": items, "cached": False, "updated_at": now, "stale": False}

    async def validate_order(self, session: AsyncSession, payload) -> None:
        """Validate new structured orders; keep older clients' text-only contract."""
        if not any((payload.shipping_settlement_ref, payload.shipping_city_ref, payload.shipping_warehouse_ref, payload.shipping_street_ref)):
            return
        if payload.shipping_company not in {"novaPost", "nova_poshta"} or not payload.shipping_settlement_ref:
            raise HTTPException(status_code=422, detail="Invalid Nova Poshta delivery selection")
        settlement = await session.get(NovaPoshtaSettlement, payload.shipping_settlement_ref)
        if settlement is None or payload.shipping_city_ref != settlement.city_ref:
            raise HTTPException(status_code=422, detail="Please select a valid delivery settlement")
        payload.shipping_city = settlement.name
        payload.shipping_area = settlement.payload["area"]
        if payload.shipping_method == "warehouse":
            branch = await session.get(NovaPoshtaBranch, payload.shipping_warehouse_ref) if payload.shipping_warehouse_ref else None
            if branch is None or branch.settlement_ref != settlement.ref:
                raise HTTPException(status_code=422, detail="Please select a valid branch in this settlement")
            payload.shipping_warehouse_number = branch.number
            payload.shipping_city_ref = branch.payload["city_ref"]
            payload.shipping_street_ref = payload.shipping_street = payload.building_number = payload.shipping_apartment = None
        elif payload.shipping_method == "courier":
            if not settlement.delivery_available or not (payload.building_number or "").strip():
                raise HTTPException(status_code=422, detail="Please enter a valid address for courier delivery")
            if payload.shipping_street_ref:
                street = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == f"np:street:{payload.shipping_street_ref}"))
                if street is None or street.payload_json.get("settlement_ref") != settlement.ref or street.expires_at <= datetime.now(UTC):
                    raise HTTPException(status_code=422, detail="Please select the delivery street again")
                payload.shipping_street = street.payload_json["label"]
            else:
                # Some courier settlements have no street directory. An explicit
                # manual address keeps the original checkout capability; absence
                # of a street Ref distinguishes it from a verified suggestion.
                payload.shipping_street = (payload.shipping_street or "").strip()
                if not payload.shipping_street:
                    raise HTTPException(status_code=422, detail="Please enter the delivery street")
            payload.shipping_warehouse_ref = payload.shipping_warehouse_number = None
        else:
            raise HTTPException(status_code=422, detail="Invalid Nova Poshta delivery method")


async def run_nova_poshta_directory_scheduler() -> None:
    directory = NovaPoshtaDirectory()
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await directory.refresh(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Nova Poshta directory refresh failed; previous snapshot retained", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(settings.nova_poshta_directory_poll_seconds)
