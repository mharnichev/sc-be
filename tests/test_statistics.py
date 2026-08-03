from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.routes import statistics as statistics_routes
from app.services.statistics import StatisticsService, divide_money, money
from app.schemas.statistics import StatisticsClientBreakdown, StatisticsServiceItem, StatisticsWorkloadDayItem


def test_money_values_are_quantized_decimal_safe() -> None:
    assert money(None) == Decimal("0.00")
    assert money("10") == Decimal("10.00")
    assert money("10.235") == Decimal("10.24")
    assert divide_money(Decimal("100.00"), 3) == Decimal("33.33")
    assert divide_money(Decimal("100.00"), 0) == Decimal("0.00")


def test_service_revenue_uses_price_saved_on_booking_item() -> None:
    expression = str(StatisticsService()._revenue_expr())

    assert "booking_service_items.price_amount" in expression


@pytest.mark.anyio
async def test_monthly_statistics_assembly_uses_completed_revenue_only(monkeypatch) -> None:
    service = StatisticsService()

    async def summary(*args, **kwargs):
        return {
            "total_income": Decimal("3000.00"),
            "completed_appointments": 3,
            "unique_clients": 2,
            "cancelled_appointments": 1,
        }

    async def services(*args, **kwargs):
        return [
            StatisticsServiceItem(service_id=1, service_name="Haircut", count=2, revenue=Decimal("2000.00")),
            StatisticsServiceItem(service_id=2, service_name="Beard", count=1, revenue=Decimal("1000.00")),
        ]

    async def workload_days(*args, **kwargs):
        return [
            StatisticsWorkloadDayItem(date="2026-05-01", completed_appointments=1, revenue=Decimal("1000.00")),
            StatisticsWorkloadDayItem(date="2026-05-02", completed_appointments=2, revenue=Decimal("2000.00")),
        ]

    async def workload_weeks(*args, **kwargs):
        return []

    async def clients(*args, **kwargs):
        return StatisticsClientBreakdown(new_clients=1, returning_clients=1)

    monkeypatch.setattr(service, "_summary", summary)
    monkeypatch.setattr(service, "_service_breakdown", services)
    monkeypatch.setattr(service, "_workload_by_day", workload_days)
    monkeypatch.setattr(service, "_workload_by_week", workload_weeks)
    monkeypatch.setattr(service, "_client_breakdown", clients)

    response = await service._get_monthly_statistics(
        session=SimpleNamespace(),
        year=2026,
        month=5,
        barber_id=7,
        master=SimpleNamespace(id=7, full_name="Gleb"),
    )

    assert response.total_income == Decimal("3000.00")
    assert response.completed_appointments == 3
    assert response.total_services_performed == 3
    assert response.average_check_per_appointment == Decimal("1000.00")
    assert response.average_revenue_per_client == Decimal("1500.00")
    assert response.cancelled_appointments == 1
    assert response.no_show_appointments == 0
    assert response.tips == Decimal("0.00")
    assert response.bonuses == Decimal("0.00")
    assert response.best_revenue_day is not None
    assert response.best_revenue_day.date == "2026-05-02"
    assert len(response.workload_by_day) == 31
    assert response.workload_by_day[2].completed_appointments == 0
    assert response.most_popular_services[0].service_name == "Haircut"
    assert response.revenue_by_service[0].revenue == Decimal("2000.00")


@pytest.mark.anyio
async def test_barber_cannot_view_another_barbers_statistics(monkeypatch) -> None:
    class FakeStatisticsService:
        async def get_linked_master_or_403(self, session, admin_user_id):
            return SimpleNamespace(id=1)

        async def get_barber_monthly_statistics(self, *args, **kwargs):
            raise AssertionError("statistics should not be loaded after failed authorization")

    monkeypatch.setattr(statistics_routes, "statistics_service", FakeStatisticsService())

    with pytest.raises(HTTPException) as exc_info:
        await statistics_routes.get_barber_monthly_statistics(
            barber_id=2,
            year=2026,
            month=5,
            current_user=SimpleNamespace(id=10, is_superuser=False),
            session=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 403
