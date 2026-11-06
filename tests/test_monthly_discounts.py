"""Monthly service discounts through HTTP and real, isolated in-memory SQL."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.v1.routes import statistics as routes
from app.core.database import get_db_session
from app.core.security import create_scoped_access_token
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master


KYIV = ZoneInfo("Europe/Kyiv")
PATH = "/api/v1/backoffice/statistics/admin/discounts/monthly"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class AsyncReadSession:
    def __init__(self, session):
        self.session = session
        self.reads = 0

    async def execute(self, statement):
        self.reads += 1
        return self.session.execute(statement)


@pytest.fixture
def report():
    engine = create_engine("sqlite://")
    for table in (Master.__table__, BarberService.__table__, Booking.__table__, BookingServiceItem.__table__):
        table.create(engine)
    with Session(engine) as session:
        session.add(Master(id=7, full_name="Inactive master", is_active=False, show_on_master_block=False))
        session.add_all([
            BarberService(id=1, master_id=7, name="Haircut", duration_minutes=30, price=9999),
            BarberService(id=2, master_id=7, name="Beard", duration_minutes=30, price=8888),
        ])
        session.commit()
        database = AsyncReadSession(session)
        app = FastAPI()
        app.include_router(routes.backoffice_router, prefix="/api/v1/backoffice")

        async def db():
            yield database

        app.dependency_overrides[get_db_session] = db
        app.dependency_overrides[get_current_admin_user] = lambda: AdminUser(id=99, is_superuser=True)
        yield app, session, database
    engine.dispose()


def visit(session, identifier, *, promo=90, manual=10, subtotal=1000, total=900,
          start=None, status=BookingStatus.completed, **fields):
    start = start or datetime(2026, 10, 15, 12, tzinfo=KYIV)
    booking = Booking(
        id=identifier, master_id=7, service_id=1, customer_id=None,
        customer_name="Saved customer", customer_phone="+380500000001",
        start_at=start, end_at=start + timedelta(minutes=30), status=status,
        completed_at=datetime(2026, 11, 5, 20, tzinfo=KYIV),
        subtotal_amount=subtotal, promotion_discount_amount=promo,
        manual_discount_amount=manual, total_amount=total,
        promotion_name_uk_snapshot="Збережена знижка",
        promotion_name_en_snapshot="Saved discount", **fields,
    )
    session.add(booking)
    session.commit()
    return booking


async def get_report(app, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(PATH, params={"year": 2026, "month": 10, **params})


@pytest.mark.anyio
async def test_saved_booking_discount_counted_once_without_current_prices_or_customer_join(report):
    app, session, database = report
    visit(session, 1)
    session.add_all([
        BookingServiceItem(booking_id=1, service_id=1, position=0, price_amount=600),
        BookingServiceItem(booking_id=1, service_id=2, position=1, price_amount=400),
    ])
    session.commit()
    visit(session, 2, promo=0, manual=0, total=1000)
    response = await get_report(app)
    assert response.status_code == 200
    value = response.json()
    assert value["period"] == {"year": 2026, "month": 10, "timezone": "Europe/Kyiv", "time_basis": "start_at"}
    assert value["scope"] == "booking_services"
    assert value["historical_completeness"] == "not_verifiable"
    assert datetime.fromisoformat(value["generated_at"]).tzinfo is not None
    assert value["summary"] == {
        "completed_bookings": 2, "discounted_bookings": 1,
        "unknown_discount_bookings": 0, "invalid_discount_bookings": 0,
        "known_discount_amount": "100.00",
    }
    assert (value["total"], value["offset"], value["limit"]) == (1, 0, 50)
    item = value["items"][0]
    assert item["booking_id"] == 1
    assert item["customer_id"] is None
    assert item["customer_name"] == "Saved customer"
    assert item["master_name"] == "Inactive master"
    assert (item["subtotal_amount"], item["promotion_discount_amount"], item["manual_discount_amount"],
            item["discount_amount"], item["total_amount"]) == ("1000.00", "90.00", "10.00", "100.00", "900.00")
    assert item["promotion_name_uk"] == "Збережена знижка"
    assert item["promotion_id"] is None
    assert item["promotion_code"] is None
    assert item["amount_status"] == "known"
    assert item["warnings"] == []
    assert database.reads == 1


@pytest.mark.anyio
async def test_unknown_invalid_and_manual_only_discounts_are_preserved(report):
    app, session, _ = report
    visit(session, 1, promo=None, manual=0, subtotal=None, total=None)
    visit(session, 2, promo=-1, manual=0)
    visit(session, 3, promo=0, manual=25, total=975)
    response = await get_report(app)
    assert response.status_code == 200
    value = response.json()
    assert value["summary"] == {
        "completed_bookings": 3, "discounted_bookings": 1,
        "unknown_discount_bookings": 1, "invalid_discount_bookings": 1,
        "known_discount_amount": "25.00",
    }
    assert value["total"] == 3
    rows = {item["booking_id"]: item for item in value["items"]}
    assert rows[1]["promotion_discount_amount"] is None
    assert rows[1]["manual_discount_amount"] == "0.00"
    assert rows[1]["discount_amount"] is None
    assert rows[1]["amount_status"] == "unknown"
    assert rows[1]["warnings"] == ["missing_pricing_snapshot"]
    assert rows[2]["discount_amount"] is None
    assert rows[2]["promotion_discount_amount"] == "-1.00"
    assert rows[2]["amount_status"] == "invalid"
    assert rows[3]["discount_amount"] == "25.00"


@pytest.mark.anyio
async def test_missing_promo_is_not_inferred_from_totals_or_the_known_manual_component(report):
    app, session, _ = report
    visit(session, 1, promo=None, manual=25, subtotal=1000, total=500)
    response = await get_report(app)
    assert response.status_code == 200
    value = response.json()
    assert value["summary"]["known_discount_amount"] == "0.00"
    assert value["summary"]["unknown_discount_bookings"] == 1
    assert value["items"][0]["manual_discount_amount"] == "25.00"
    assert value["items"][0]["discount_amount"] is None
    assert value["items"][0]["amount_status"] == "unknown"


@pytest.mark.anyio
async def test_completed_start_month_excludes_archives_other_statuses_and_other_months(report):
    app, session, _ = report
    visit(session, 1, start=datetime(2026, 10, 1, 0, tzinfo=KYIV))
    visit(session, 2, start=datetime(2026, 10, 31, 23, 59, 59, tzinfo=KYIV))
    visit(session, 3, start=datetime(2026, 9, 30, 23, 59, 59, tzinfo=KYIV))
    visit(session, 4, start=datetime(2026, 11, 1, 0, tzinfo=KYIV))
    visit(session, 5, archived_at=datetime(2026, 11, 1, tzinfo=KYIV))
    for identifier, status in enumerate(
        [BookingStatus.pending, BookingStatus.confirmed, BookingStatus.cancelled, BookingStatus.no_show], 6,
    ):
        visit(session, identifier, status=status)
    response = await get_report(app)
    assert response.status_code == 200
    value = response.json()
    assert [item["booking_id"] for item in value["items"]] == [2, 1]
    assert value["summary"]["completed_bookings"] == 2
    assert value["summary"]["known_discount_amount"] == "200.00"


@pytest.mark.anyio
async def test_pagination_keeps_full_summary_and_tie_breaker_even_beyond_last_page(report):
    app, session, _ = report
    for identifier in (1, 2, 3):
        visit(session, identifier)
    response = await get_report(app, offset=1, limit=1)
    assert response.status_code == 200
    value = response.json()
    assert [item["booking_id"] for item in value["items"]] == [2]
    assert (value["total"], value["offset"], value["limit"]) == (3, 1, 1)
    assert value["summary"]["known_discount_amount"] == "300.00"
    response = await get_report(app, offset=10, limit=1)
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 3
    assert response.json()["summary"]["known_discount_amount"] == "300.00"


@pytest.mark.anyio
async def test_empty_month_is_typed_and_has_zero_known_sum(report):
    app, _, _ = report
    response = await get_report(app)
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    assert response.json()["summary"] == {
        "completed_bookings": 0, "discounted_bookings": 0,
        "unknown_discount_bookings": 0, "invalid_discount_bookings": 0,
        "known_discount_amount": "0.00",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("params", [
    {"year": 1999}, {"year": 2101}, {"month": 0}, {"month": 13},
    {"offset": -1}, {"limit": 0}, {"limit": 101}, {"month": "invalid"},
])
async def test_invalid_query_is_rejected_without_loading_report(report, params):
    app, _, database = report
    response = await get_report(app, **params)
    assert response.status_code == 422
    assert database.reads == 0


@pytest.mark.anyio
async def test_non_superuser_is_forbidden_without_reading_customer_data(report):
    app, _, database = report
    app.dependency_overrides[get_current_admin_user] = lambda: AdminUser(id=2, is_superuser=False)
    response = await get_report(app)
    assert response.status_code == 403
    assert database.reads == 0


@pytest.mark.anyio
@pytest.mark.parametrize("authorization", [None, "Bearer malformed"])
async def test_missing_and_invalid_authentication_are_unauthorized(report, authorization):
    app, _, database = report
    app.dependency_overrides.pop(get_current_admin_user)
    headers = {"Authorization": authorization} if authorization else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, params={"year": 2026, "month": 10}, headers=headers)
    assert response.status_code == 401
    assert database.reads == 0


@pytest.mark.anyio
async def test_customer_scope_cannot_read_global_customer_discount_data(report):
    app, _, database = report
    app.dependency_overrides.pop(get_current_admin_user)
    token = create_scoped_access_token(subject="99", scope="customer")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, params={"year": 2026, "month": 10},
                                    headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert database.reads == 0


@pytest.mark.anyio
async def test_price_anomalies_are_marked_without_rewriting_recorded_discounts(report):
    app, session, _ = report
    visit(session, 1, subtotal=100, promo=100, manual=20, total=0)
    visit(session, 2, subtotal=1000, promo=100, manual=0, total=1000)
    visit(session, 3, subtotal=-10, promo=10, manual=0, total=-1)
    visit(session, 4, promo=2_000_000_000, manual=2_000_000_000, total=0)
    response = await get_report(app)
    assert response.status_code == 200
    rows = {item["booking_id"]: item for item in response.json()["items"]}
    assert rows[1]["discount_amount"] == "120.00"
    assert rows[1]["warnings"] == ["discount_exceeds_subtotal"]
    assert rows[2]["warnings"] == ["snapshot_total_mismatch"]
    assert rows[3]["warnings"] == ["invalid_pricing_snapshot"]
    assert rows[4]["discount_amount"] == "4000000000.00"


def test_openapi_exposes_accepted_query_and_decimal_string_contract(report):
    app, _, _ = report
    schema = app.openapi()
    operation = schema["paths"][PATH]["get"]
    assert {param["name"] for param in operation["parameters"]} == {"year", "month", "offset", "limit"}
    assert operation["security"] == [{"OAuth2PasswordBearer": []}]
    item = schema["components"]["schemas"]["MonthlyDiscountItem"]
    assert {"type": "string"} in item["properties"]["discount_amount"]["anyOf"]
    assert {"type": "null"} in item["properties"]["discount_amount"]["anyOf"]


def test_postgresql_month_bounds_preserve_kyiv_offsets_and_december_rollover():
    from datetime import UTC

    from sqlalchemy.dialects import postgresql

    from app.services.monthly_discounts import report_statement
    from app.services.statistics import StatisticsService

    start, end = StatisticsService().month_bounds(2026, 10)
    assert start.astimezone(UTC) == datetime(2026, 9, 30, 21, tzinfo=UTC)
    assert end.astimezone(UTC) == datetime(2026, 10, 31, 22, tzinfo=UTC)
    compiled = report_statement(start, end, offset=0, limit=50).compile(dialect=postgresql.dialect())
    assert compiled.params["start_at_1"] == start
    assert compiled.params["start_at_2"] == end
    assert "booking_service_items" not in str(compiled)
    start, end = StatisticsService().month_bounds(2026, 12)
    assert start == datetime(2026, 12, 1, tzinfo=KYIV)
    assert end == datetime(2027, 1, 1, tzinfo=KYIV)
