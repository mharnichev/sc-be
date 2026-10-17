"""Provider snapshot contracts, independent of network and database."""
from unittest.mock import AsyncMock

import pytest

from app.services.nova_poshta_directory import NovaPoshtaDirectory, normalize, truth


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settlement(ref="settlement", name="Київ", **extra):
    return {"Ref": ref, "Description": name, "DescriptionRu": "Киев", "DescriptionTranslit": "Kyiv",
            "SettlementTypeDescription": "місто", "AreaDescription": "Київська", "RegionsDescription": "",
            "AddressDeliveryAllowed": "1", **extra}


def branch(ref="branch", settlement_ref="settlement", number="2", **extra):
    return {"Ref": ref, "SettlementRef": settlement_ref, "CityRef": "delivery-city", "Number": number,
            "Description": f"Відділення №{number}: вул. Хрещатик", "ShortAddress": "вул. Хрещатик",
            "WarehouseStatus": "Working", **extra}


def test_prepare_keeps_provider_references_distinct_and_filters_unavailable_points():
    cities, points = NovaPoshtaDirectory.prepare(
        [settlement(), settlement("village", SettlementTypeDescription="село", AddressDeliveryAllowed="0")],
        [branch(), branch("closed", WarehouseStatus="Closed"), branch("denied", DenyToSelect="1"),
         branch("locker", number="123", CategoryOfWarehouse="Postomat")],
    )
    assert cities[0]["ref"] == "settlement"
    assert cities[0]["city_ref"] == "delivery-city"
    assert cities[0]["popular_rank"] == 0
    assert cities[1]["popular_rank"] == 100
    assert cities[1]["delivery_available"] is False
    assert [item["ref"] for item in points] == ["branch", "locker"]
    assert points[1]["payload"]["type"] == "postomat"
    assert "киев" in cities[0]["search_text"] and "kyiv" in cities[0]["search_text"]


@pytest.mark.parametrize("value,expected", [(True, True), ("1", True), ("true", True), (False, False), ("0", False), ("false", False), (None, False)])
def test_provider_flags_do_not_use_python_string_truthiness(value, expected):
    assert truth(value) is expected


def test_normalization_handles_whitespace_case_and_apostrophe_variants():
    assert normalize("  КАМʼЯНЕЦЬ  Подільський ") == "кам'янець подільський"
    assert normalize("Кам’янець") == normalize("Кам'янець")


def test_snapshot_rejects_orphan_branches():
    with pytest.raises(ValueError, match="unknown settlement"):
        NovaPoshtaDirectory.prepare([settlement()], [branch(settlement_ref="other")])


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["getWarehouses", "getSettlements"])
async def test_pagination_collects_every_page_and_checks_total(monkeypatch, method):
    service = NovaPoshtaDirectory()
    service.provider._request_api = AsyncMock(side_effect=[
        {"data": [{"Ref": "a"}], "info": {"totalCount": "2"}},
        {"data": [{"Ref": "b"}], "info": {"totalCount": "2"}},
    ])
    monkeypatch.setattr("app.services.nova_poshta_directory.asyncio.sleep", AsyncMock())
    assert await service._pages(method) == [{"Ref": "a"}, {"Ref": "b"}]
    assert service.provider._request_api.call_args.kwargs["method_properties"]["Page"] == "2"
    assert all(call.kwargs["method_properties"]["Limit"] == "500"
               for call in service.provider._request_api.await_args_list)


@pytest.mark.anyio
@pytest.mark.parametrize("second,error", [([], "Incomplete"), ([{"Ref": "a"}], "Repeated")])
async def test_pagination_refuses_partial_or_repeated_snapshot(second, error, monkeypatch):
    service = NovaPoshtaDirectory()
    service.provider._request_api = AsyncMock(side_effect=[
        {"data": [{"Ref": "a"}], "info": {"totalCount": 2}},
        {"data": second, "info": {"totalCount": 2}},
    ])
    monkeypatch.setattr("app.services.nova_poshta_directory.asyncio.sleep", AsyncMock())
    with pytest.raises(ValueError, match=error):
        await service._pages("getSettlements")


@pytest.mark.anyio
async def test_pagination_retries_rate_limit_and_transient_failure_only(monkeypatch):
    from fastapi import HTTPException
    service = NovaPoshtaDirectory()
    service.provider._request_api = AsyncMock(side_effect=[
        HTTPException(502, "To many requests"), HTTPException(503, "offline"),
        {"data": [{"Ref": "a"}], "info": {"totalCount": 1}},
    ])
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.nova_poshta_directory.asyncio.sleep", sleep)
    assert await service._pages("getSettlements") == [{"Ref": "a"}]
    assert [call.args[0] for call in sleep.await_args_list] == [60, 4]
    service.provider._request_api = AsyncMock(side_effect=HTTPException(502, "Invalid key"))
    with pytest.raises(HTTPException):
        await service._pages("getSettlements")
    assert service.provider._request_api.await_count == 1


def test_public_routes_validate_queries_and_return_stable_empty_contract(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1.routes import delivery
    from app.core.database import get_db_session

    app = FastAPI()
    app.include_router(delivery.public_router)

    async def fake_session():
        yield object()

    app.dependency_overrides[get_db_session] = fake_session
    settlements = AsyncMock(return_value={"items": [], "cached": True, "updated_at": None, "stale": False})
    monkeypatch.setattr(delivery.directory, "settlements", settlements)
    client = TestClient(app)
    assert client.get("/np/settlements").json() == {"items": [], "cached": True, "updated_at": None, "stale": False}
    for params in ({"limit": 0}, {"limit": 51}, {"q": "x" * 121}):
        assert client.get("/np/settlements", params=params).status_code == 422
    ref = "00000000-0000-0000-0000-000000000000"
    for q in ("", "x", "  "):
        assert client.get("/np/streets", params={"settlement_ref": ref, "q": q}).status_code == 422
    assert client.get("/np/branches", params={"settlement_ref": "invalid"}).status_code == 422
