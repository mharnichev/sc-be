from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class StatisticsBarberSummary(BaseModel):
    id: int
    full_name: str


class StatisticsServiceItem(BaseModel):
    service_id: int
    service_name: str
    count: int
    revenue: Decimal = Field(max_digits=12, decimal_places=2)


class StatisticsCategoryItem(BaseModel):
    category: str
    count: int
    revenue: Decimal = Field(max_digits=12, decimal_places=2)


class StatisticsWorkloadDayItem(BaseModel):
    date: str
    completed_appointments: int
    revenue: Decimal = Field(max_digits=12, decimal_places=2)


class StatisticsWorkloadWeekItem(BaseModel):
    week: int
    completed_appointments: int
    revenue: Decimal = Field(max_digits=12, decimal_places=2)


class StatisticsClientBreakdown(BaseModel):
    new_clients: int
    returning_clients: int


class BarberMonthlyStatisticsResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "year": 2026,
                    "month": 5,
                    "barber": {"id": 7, "full_name": "Gleb"},
                    "total_income": "12400.00",
                    "completed_appointments": 18,
                    "unique_clients": 14,
                    "total_services_performed": 18,
                    "most_popular_services": [
                        {"service_id": 3, "service_name": "Haircut", "count": 9, "revenue": "8100.00"}
                    ],
                    "revenue_by_service": [
                        {"service_id": 3, "service_name": "Haircut", "count": 9, "revenue": "8100.00"}
                    ],
                    "average_check_per_appointment": "688.89",
                    "average_revenue_per_client": "885.71",
                    "clients": {"new_clients": 5, "returning_clients": 9},
                    "cancelled_appointments": 2,
                    "no_show_appointments": 0,
                    "workload_by_day": [
                        {"date": "2026-05-12", "completed_appointments": 3, "revenue": "2100.00"}
                    ],
                    "workload_by_week": [{"week": 20, "completed_appointments": 6, "revenue": "4200.00"}],
                    "best_revenue_day": {
                        "date": "2026-05-12",
                        "completed_appointments": 3,
                        "revenue": "2100.00",
                    },
                    "service_category_breakdown": [],
                    "tips": "0.00",
                    "bonuses": "0.00",
                }
            ]
        }
    )

    year: int
    month: int
    barber: StatisticsBarberSummary | None = None
    total_income: Decimal = Field(max_digits=12, decimal_places=2)
    completed_appointments: int
    unique_clients: int
    total_services_performed: int
    most_popular_services: list[StatisticsServiceItem]
    revenue_by_service: list[StatisticsServiceItem]
    average_check_per_appointment: Decimal = Field(max_digits=12, decimal_places=2)
    average_revenue_per_client: Decimal = Field(max_digits=12, decimal_places=2)
    clients: StatisticsClientBreakdown
    cancelled_appointments: int
    no_show_appointments: int
    workload_by_day: list[StatisticsWorkloadDayItem]
    workload_by_week: list[StatisticsWorkloadWeekItem]
    best_revenue_day: StatisticsWorkloadDayItem | None
    service_category_breakdown: list[StatisticsCategoryItem]
    tips: Decimal = Field(max_digits=12, decimal_places=2)
    bonuses: Decimal = Field(max_digits=12, decimal_places=2)


class BarberComparisonItem(BaseModel):
    barber: StatisticsBarberSummary
    revenue: Decimal = Field(max_digits=12, decimal_places=2)
    unique_clients: int
    completed_appointments: int
    average_check: Decimal = Field(max_digits=12, decimal_places=2)
    popular_services: list[StatisticsServiceItem]


class AdminMonthlyStatisticsResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "year": 2026,
                    "month": 5,
                    "barber_id": None,
                    "total_barbershop_monthly_revenue": "46000.00",
                    "total_clients": 52,
                    "total_completed_appointments": 71,
                    "total_cancelled_appointments": 6,
                    "aggregate": {
                        "year": 2026,
                        "month": 5,
                        "barber": None,
                        "total_income": "46000.00",
                        "completed_appointments": 71,
                        "unique_clients": 52,
                        "total_services_performed": 71,
                        "most_popular_services": [],
                        "revenue_by_service": [],
                        "average_check_per_appointment": "647.89",
                        "average_revenue_per_client": "884.62",
                        "clients": {"new_clients": 20, "returning_clients": 32},
                        "cancelled_appointments": 6,
                        "no_show_appointments": 0,
                        "workload_by_day": [],
                        "workload_by_week": [],
                        "best_revenue_day": None,
                        "service_category_breakdown": [],
                        "tips": "0.00",
                        "bonuses": "0.00",
                    },
                    "top_barbers": [],
                    "most_popular_services": [],
                }
            ]
        }
    )

    year: int
    month: int
    barber_id: int | None = None
    total_barbershop_monthly_revenue: Decimal = Field(max_digits=12, decimal_places=2)
    total_clients: int
    total_completed_appointments: int
    total_cancelled_appointments: int
    aggregate: BarberMonthlyStatisticsResponse
    top_barbers: list[BarberComparisonItem]
    most_popular_services: list[StatisticsServiceItem]


class BarbersComparisonResponse(BaseModel):
    year: int
    month: int
    barbers: list[BarberComparisonItem]
    top_performing_barbers: list[BarberComparisonItem]
