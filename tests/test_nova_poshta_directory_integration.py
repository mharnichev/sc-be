"""Nova Poshta contracts against isolated, opt-in local PostgreSQL schemas."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.nova_poshta import NovaPoshtaBranch, NovaPoshtaSettlement
from app.models.shop import DeliveryCache
from app.schemas.order import OrderCreate
from app.services.nova_poshta_directory import DIRECTORY_KEY, NovaPoshtaDirectory
from test_inventory_integration import database  # noqa: F401 -- guarded local test schema fixture
from test_nova_poshta_directory import branch, settlement


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def seed(database):
    cities, branches = NovaPoshtaDirectory.prepare(
        [settlement(), settlement("other", "Львів"), settlement("village", "Київ", SettlementTypeDescription="село")],
        [branch(), branch("b12", number="12"), branch("b102", number="102"),
         branch("other-branch", "other", "1", CityRef="other-city")],
    )
    async with database() as session:
        session.add_all(NovaPoshtaSettlement(**row) for row in cities)
        session.add_all(NovaPoshtaBranch(**row) for row in branches)
        session.add(DeliveryCache(cache_key=DIRECTORY_KEY, payload_json={}, expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await session.commit()


def payload(**changes):
    return SimpleNamespace(shipping_company="novaPost", shipping_method="warehouse", shipping_settlement_ref="settlement",
                           shipping_city_ref="delivery-city", shipping_warehouse_ref="branch", shipping_street_ref=None,
                           shipping_city="forged", shipping_area="forged", shipping_warehouse_number="forged",
                           shipping_street="forged", building_number="12", shipping_apartment="1", **changes)


@pytest.mark.anyio
async def test_local_search_popular_aliases_exact_branch_and_literal_wildcards(database):
    await seed(database)
    service = NovaPoshtaDirectory()
    async with database() as session:
        popular = await service.settlements(session)
        assert [row["ref"] for row in popular["items"]] == ["settlement", "other"]
        assert popular["stale"] is True
        assert (await service.settlements(session, "киев"))["items"]
        assert (await service.settlements(session, "KYIV"))["items"]
        for malicious in ("%", "_", "' OR 1=1 --"):
            assert (await service.settlements(session, malicious))["items"] == []
        result = await service.branches(session, "settlement", "№ 2")
        assert result["items"][0]["ref"] == "branch"
        assert {row["ref"] for row in result["items"]} == {"branch", "b12", "b102"}
        with pytest.raises(HTTPException) as error:
            await service.branches(session, "missing")
        assert error.value.status_code == 404


@pytest.mark.anyio
async def test_refresh_failure_retains_snapshot_and_success_replaces_atomically(database, monkeypatch):
    await seed(database)
    monkeypatch.setattr("app.services.nova_poshta_directory.settings.nova_poshta_api_key", "test-key")
    service = NovaPoshtaDirectory()
    service._pages = AsyncMock(side_effect=[[settlement("replacement", "Одеса")], ValueError("truncated")])
    async with database() as session:
        with pytest.raises(ValueError, match="truncated"):
            await service.refresh(session, force=True)
        assert await session.get(NovaPoshtaSettlement, "settlement") is not None
        assert await session.get(NovaPoshtaBranch, "branch") is not None
    service._pages = AsyncMock(side_effect=[[settlement("replacement", "Одеса")], [branch("new", "replacement")]])
    async with database() as session:
        assert await service.refresh(session, force=True) is True
        assert await session.get(NovaPoshtaSettlement, "settlement") is None
        assert await session.get(NovaPoshtaBranch, "branch") is None
        assert await session.get(NovaPoshtaBranch, "new") is not None
        assert await service.refresh(session) is False
        assert service._pages.await_count == 2


@pytest.mark.anyio
async def test_street_cache_stale_fallback_provenance_and_courier_validation(database, monkeypatch):
    await seed(database)
    monkeypatch.setattr("app.services.nova_poshta_directory.settings.nova_poshta_api_key", "test-key")
    service = NovaPoshtaDirectory()
    service.provider._call_api = AsyncMock(return_value=[{"Addresses": [{
        "SettlementRef": "settlement", "SettlementStreetRef": "street", "SettlementStreetDescription": "Хрещатик",
        "Present": "вул. Хрещатик",
    }]}])
    async with database() as session:
        first = await service.streets(session, "settlement", "Хр")
        assert first["cached"] is False
        assert (await service.streets(session, "settlement", "хр"))["cached"] is True
        assert service.provider._call_api.await_count == 1
        proof = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == "np:street:street"))
        assert proof.payload_json["settlement_ref"] == "settlement"
        order = payload()
        order.shipping_method, order.shipping_street_ref = "courier", "street"
        await service.validate_order(session, order)
        assert order.shipping_street == "вул. Хрещатик"
        assert order.shipping_warehouse_ref is None and order.shipping_city == "Київ"
        cache = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == "np:streets:settlement:хр:20"))
        cache.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        service.provider._call_api = AsyncMock(side_effect=HTTPException(503, "offline"))
        assert (await service.streets(session, "settlement", "Хр"))["stale"] is True
        proof.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        with pytest.raises(HTTPException) as error:
            await service.validate_order(session, order)
        assert error.value.status_code == 422
        cache.updated_at = datetime.now(UTC) - timedelta(days=8)
        await session.commit()
        with pytest.raises(HTTPException) as error:
            await service.streets(session, "settlement", "Хр")
        assert error.value.status_code == 503


@pytest.mark.anyio
async def test_order_rejects_wrong_city_branch_unavailable_courier_and_missing_building(database):
    await seed(database)
    service = NovaPoshtaDirectory()
    async with database() as session:
        order = payload()
        await service.validate_order(session, order)
        assert (order.shipping_city, order.shipping_area, order.shipping_warehouse_number) == ("Київ", "Київська", "2")
        assert order.shipping_street is None and order.building_number is None
        cases = [dict(shipping_city_ref="other-city"), dict(shipping_warehouse_ref="other-branch"),
                 dict(shipping_method="courier", shipping_street_ref="missing"),
                 dict(shipping_method="courier", shipping_street_ref="street", building_number=" ")]
        for changes in cases:
            invalid = payload()
            for key, value in changes.items():
                setattr(invalid, key, value)
            with pytest.raises(HTTPException) as error:
                await service.validate_order(session, invalid)
            assert error.value.status_code == 422
        city = await session.get(NovaPoshtaSettlement, "settlement")
        city.delivery_available = False
        with pytest.raises(HTTPException) as error:
            await service.streets(session, "settlement", "Хр")
        assert error.value.status_code == 422
        legacy = payload()
        legacy.shipping_settlement_ref = legacy.shipping_city_ref = legacy.shipping_warehouse_ref = None
        await service.validate_order(session, legacy)


def test_checkout_schema_accepts_structured_camel_case_references():
    refs = tuple(f"00000000-0000-0000-0000-{i:012d}" for i in range(4))
    order = OrderCreate(firstName="Jane", phoneNumber="+380501112233", items=[{"productId": 1, "quantity": 1}],
                        shippingSettlementRef=refs[0], shippingCityRef=refs[1], shippingWarehouseRef=refs[2], shippingStreetRef=refs[3])
    assert (order.shipping_settlement_ref, order.shipping_city_ref, order.shipping_warehouse_ref, order.shipping_street_ref) == refs


@pytest.mark.anyio
async def test_street_response_cannot_prove_another_settlements_address(database, monkeypatch):
    await seed(database)
    monkeypatch.setattr("app.services.nova_poshta_directory.settings.nova_poshta_api_key", "test-key")
    service = NovaPoshtaDirectory()
    service.provider._call_api = AsyncMock(return_value=[{"Addresses": [{
        "SettlementRef": "other", "SettlementStreetRef": "foreign-street", "SettlementStreetDescription": "Wrong",
        "Present": "Wrong settlement",
    }]}])
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.streets(session, "settlement", "Wr")
        assert error.value.status_code == 502
        assert await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == "np:street:foreign-street")) is None


@pytest.mark.anyio
async def test_refresh_write_failure_rolls_back_deletes_and_preserves_metadata(database, monkeypatch):
    await seed(database)
    monkeypatch.setattr("app.services.nova_poshta_directory.settings.nova_poshta_api_key", "test-key")
    service = NovaPoshtaDirectory()
    # Duplicate provider refs violate the database PK only after the old rows were deleted.
    service._pages = AsyncMock(side_effect=[
        [settlement("replacement", "Одеса"), settlement("replacement", "Одеса")],
        [branch("new", "replacement")],
    ])
    from sqlalchemy.exc import IntegrityError
    async with database() as session:
        before = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == DIRECTORY_KEY))
        before_timestamp = before.expires_at
        with pytest.raises(IntegrityError):
            await service.refresh(session, force=True)
        assert await session.get(NovaPoshtaSettlement, "settlement") is not None
        assert await session.get(NovaPoshtaBranch, "branch") is not None
        assert await session.get(NovaPoshtaSettlement, "replacement") is None
        after = await session.scalar(select(DeliveryCache).where(DeliveryCache.cache_key == DIRECTORY_KEY))
        assert after.expires_at == before_timestamp


@pytest.mark.anyio
async def test_empty_directory_is_explicitly_unavailable_and_missing_key_never_fetches(database, monkeypatch):
    service = NovaPoshtaDirectory()
    service._pages = AsyncMock()
    monkeypatch.setattr("app.services.nova_poshta_directory.settings.nova_poshta_api_key", "")
    async with database() as session:
        assert await service.refresh(session) is False
        service._pages.assert_not_awaited()
        with pytest.raises(HTTPException) as error:
            await service.settlements(session, "Київ")
        assert error.value.status_code == 503


@pytest.mark.anyio
async def test_parallel_refresh_skips_worker_when_snapshot_lock_is_held(database, monkeypatch):
    from sqlalchemy import text
    monkeypatch.setattr("app.services.nova_poshta_directory.settings.nova_poshta_api_key", "test-key")
    service = NovaPoshtaDirectory()
    service._pages = AsyncMock()
    async with database() as first, database() as second:
        assert await first.scalar(text("SELECT pg_try_advisory_xact_lock(731028410)")) is True
        assert await service.refresh(second, force=True) is False
        service._pages.assert_not_awaited()


@pytest.mark.anyio
async def test_parallel_street_search_fetches_provider_once(database, monkeypatch):
    import asyncio
    await seed(database)
    monkeypatch.setattr("app.services.nova_poshta_directory.settings.nova_poshta_api_key", "test-key")
    service = NovaPoshtaDirectory()
    service.provider._call_api = AsyncMock(return_value=[{"Addresses": [{
        "SettlementRef": "settlement", "SettlementStreetRef": "street", "SettlementStreetDescription": "Хрещатик",
        "Present": "вул. Хрещатик",
    }]}])

    async def search():
        async with database() as session:
            return await service.streets(session, "settlement", "Хр")

    first, second = await asyncio.wait_for(asyncio.gather(search(), search()), timeout=5)
    assert first["items"] == second["items"]
    assert sorted([first["cached"], second["cached"]]) == [False, True]
    assert service.provider._call_api.await_count == 1


@pytest.mark.anyio
async def test_manual_courier_address_preserves_delivery_without_street_directory(database):
    await seed(database)
    service = NovaPoshtaDirectory()
    async with database() as session:
        order = payload()
        order.shipping_method = "courier"
        order.shipping_street_ref = None
        order.shipping_street = "  Нова вулиця  "
        order.shipping_city_ref = None
        city = await session.get(NovaPoshtaSettlement, "settlement")
        city.city_ref = None  # Courier-only settlement with no branch city mapping.
        await service.validate_order(session, order)
        assert order.shipping_street == "Нова вулиця"
        assert order.shipping_street_ref is None and order.shipping_warehouse_ref is None
        assert order.shipping_city == "Київ"
        order.shipping_street = "   "
        with pytest.raises(HTTPException) as error:
            await service.validate_order(session, order)
        assert error.value.status_code == 422
        order.shipping_street = "Нова вулиця"
        order.shipping_street_ref = "forged-street-reference"
        with pytest.raises(HTTPException) as error:
            await service.validate_order(session, order)
        assert error.value.status_code == 422
