from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.booking_funnel import BookingFunnelAggregate


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


class DashboardDatePeriod(BaseModel):
    date_from: date
    date_to: date
    days: int


class DashboardDefinitions(BaseModel):
    gross_revenue: str
    available_minutes: str
    booked_minutes: str
    cancellation_rate: str
    retention_cohort: str
    service_allocation: str
    no_show: str
    prime_time: str


class DashboardSignalThresholds(BaseModel):
    pending_bookings_min_count: int
    cancellation_min_count: int
    cancellation_min_rate_percent: Decimal = Field(max_digits=7, decimal_places=2)
    cancellation_min_increase_percentage_points: Decimal = Field(max_digits=7, decimal_places=2)
    unfilled_capacity_min_minutes: int
    unfilled_capacity_min_percent: Decimal = Field(max_digits=7, decimal_places=2)
    review_moderation_backlog_min_count: int
    failed_review_delivery_min_count: int


class DashboardPeriodMetadata(BaseModel):
    current: DashboardDatePeriod
    previous: DashboardDatePeriod | None
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    applied_master_id: int | None
    comparison_requested: bool
    max_range_days: int
    definitions: DashboardDefinitions
    signal_thresholds: DashboardSignalThresholds


class DashboardMoneyMetric(BaseModel):
    current: Decimal = Field(max_digits=14, decimal_places=2)
    previous: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    percent_change: Decimal | None = Field(default=None, max_digits=9, decimal_places=2)


class DashboardCountMetric(BaseModel):
    current: int
    previous: int | None
    percent_change: Decimal | None = Field(default=None, max_digits=9, decimal_places=2)


class DashboardExecutiveMetrics(BaseModel):
    gross_revenue: DashboardMoneyMetric = Field(
        description="Completed-booking realized gross revenue. This is revenue, not profit."
    )
    completed_visits: DashboardCountMetric
    unique_clients: DashboardCountMetric = Field(
        description="Distinct customers with at least one completed visit in the period."
    )
    new_database_customers: DashboardCountMetric = Field(
        description="Distinct customer phone records first created in the database during the period."
    )
    average_check: DashboardMoneyMetric
    booking_subtotal: DashboardMoneyMetric
    promotion_discount_amount: DashboardMoneyMetric


class DashboardRateMetric(BaseModel):
    current: Decimal = Field(max_digits=7, decimal_places=2)
    previous: Decimal | None = Field(default=None, max_digits=7, decimal_places=2)
    change_percentage_points: Decimal | None = Field(default=None, max_digits=7, decimal_places=2)


class DashboardPrimeTimeWindow(BaseModel):
    master_id: int
    master_name: str
    start_at: datetime
    end_at: datetime
    available_minutes: int
    definition_code: Literal["weekday_evening", "weekend_midday"]


class DashboardCapacityLeakage(BaseModel):
    available_minutes: int
    booked_minutes: int
    utilisation_rate: Decimal = Field(max_digits=7, decimal_places=2)
    cancelled_visits: int
    cancellation_rate: DashboardRateMetric
    pending_unconfirmed_upcoming_bookings: int
    empty_upcoming_capacity_minutes: int
    empty_upcoming_capacity_rate: Decimal = Field(max_digits=7, decimal_places=2)
    prime_time_empty_windows: list[DashboardPrimeTimeWindow]
    no_show_visits: None = Field(
        default=None,
        description="Unavailable until the booking domain has a no-show status.",
    )
    no_show_status: Literal["unavailable"] = "unavailable"


class DashboardRepeatMetric(BaseModel):
    window_days: Literal[30, 45, 60]
    repeated_clients: int
    eligible_clients: int
    repeat_rate: Decimal | None = Field(default=None, max_digits=7, decimal_places=2)


class DashboardRetention(BaseModel):
    new_clients: int
    returning_clients: int
    repeat_30_day: DashboardRepeatMetric
    repeat_45_day: DashboardRepeatMetric
    repeat_60_day: DashboardRepeatMetric


class DashboardMasterBreakdownItem(BaseModel):
    master_id: int
    master_name: str
    gross_revenue: Decimal = Field(max_digits=14, decimal_places=2)
    completed_visits: int
    average_check: Decimal = Field(max_digits=14, decimal_places=2)
    available_minutes: int
    booked_minutes: int
    utilisation_rate: Decimal = Field(max_digits=7, decimal_places=2)
    revenue_per_available_hour: Decimal = Field(max_digits=14, decimal_places=2)
    new_clients: int
    returning_clients: int
    approved_rating: Decimal | None = Field(default=None, max_digits=2, decimal_places=1)
    approved_review_count: int


class DashboardServiceBreakdownItem(BaseModel):
    service_id: int
    service_name: str
    completed_visits: int
    gross_revenue: Decimal = Field(max_digits=14, decimal_places=2)
    subtotal: Decimal = Field(max_digits=14, decimal_places=2)
    discounts: Decimal = Field(max_digits=14, decimal_places=2)
    average_realized_revenue_per_completed_service: Decimal = Field(max_digits=14, decimal_places=2)


class DashboardActionSignal(BaseModel):
    severity: Literal["info", "warning", "critical"]
    code: Literal[
        "pending_bookings",
        "elevated_cancellations",
        "unfilled_capacity",
        "review_moderation_backlog",
        "failed_review_delivery",
    ]
    title_uk: str
    explanation_uk: str
    metric_value: Decimal
    metric_unit: Literal["bookings", "percentage_points", "minutes", "reviews", "deliveries"]
    recommended_backoffice_route: str


class AdminDashboardStatisticsResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "period": {
                        "current": {"date_from": "2026-06-01", "date_to": "2026-06-30", "days": 30},
                        "previous": {"date_from": "2026-05-02", "date_to": "2026-05-31", "days": 30},
                        "timezone": "Europe/Kyiv",
                        "applied_master_id": None,
                        "comparison_requested": True,
                        "max_range_days": 366,
                        "definitions": {
                            "gross_revenue": "Sum of total_amount snapshots for completed bookings; revenue is not profit.",
                            "available_minutes": "Published active-master availability intersected with the period, minus overlapping time blocks.",
                            "booked_minutes": "Union of non-cancelled booking intervals intersected with net available time.",
                            "cancellation_rate": "Cancelled bookings divided by all bookings scheduled in the period.",
                            "retention_cohort": "Clients whose first completed visit in the applied scope is in the period; each repeat denominator includes only clients fully observable for that window.",
                            "service_allocation": "Booking snapshots are allocated across services in proportion to current service prices because per-item price snapshots do not exist.",
                            "no_show": "Unavailable because BookingStatus has no no-show value.",
                            "prime_time": "Kyiv local weekday 17:00-20:00 and weekend 10:00-14:00; only empty intervals of at least 30 minutes are listed.",
                        },
                        "signal_thresholds": {
                            "pending_bookings_min_count": 1,
                            "cancellation_min_count": 3,
                            "cancellation_min_rate_percent": "15.00",
                            "cancellation_min_increase_percentage_points": "5.00",
                            "unfilled_capacity_min_minutes": 120,
                            "unfilled_capacity_min_percent": "30.00",
                            "review_moderation_backlog_min_count": 1,
                            "failed_review_delivery_min_count": 1,
                        },
                    },
                    "executive": {
                        "gross_revenue": {"current": "46000.00", "previous": "40000.00", "percent_change": "15.00"},
                        "completed_visits": {"current": 71, "previous": 64, "percent_change": "10.94"},
                        "unique_clients": {"current": 52, "previous": 49, "percent_change": "6.12"},
                        "new_database_customers": {"current": 18, "previous": 14, "percent_change": "28.57"},
                        "average_check": {"current": "647.89", "previous": "625.00", "percent_change": "3.66"},
                        "booking_subtotal": {"current": "50000.00", "previous": "43000.00", "percent_change": "16.28"},
                        "promotion_discount_amount": {
                            "current": "4000.00",
                            "previous": "3000.00",
                            "percent_change": "33.33",
                        },
                    },
                    "capacity_and_leakage": {
                        "available_minutes": 24000,
                        "booked_minutes": 15600,
                        "utilisation_rate": "65.00",
                        "cancelled_visits": 6,
                        "cancellation_rate": {
                            "current": "7.79",
                            "previous": "5.88",
                            "change_percentage_points": "1.91",
                        },
                        "pending_unconfirmed_upcoming_bookings": 2,
                        "empty_upcoming_capacity_minutes": 1800,
                        "empty_upcoming_capacity_rate": "31.25",
                        "prime_time_empty_windows": [],
                        "no_show_visits": None,
                        "no_show_status": "unavailable",
                    },
                    "retention": {
                        "new_clients": 20,
                        "returning_clients": 32,
                        "repeat_30_day": {
                            "window_days": 30,
                            "repeated_clients": 6,
                            "eligible_clients": 15,
                            "repeat_rate": "40.00",
                        },
                        "repeat_45_day": {
                            "window_days": 45,
                            "repeated_clients": 4,
                            "eligible_clients": 10,
                            "repeat_rate": "40.00",
                        },
                        "repeat_60_day": {
                            "window_days": 60,
                            "repeated_clients": 2,
                            "eligible_clients": 6,
                            "repeat_rate": "33.33",
                        },
                    },
                    "masters": [],
                    "services": [],
                    "actionable_signals": [],
                }
            ]
        }
    )

    period: DashboardPeriodMetadata
    executive: DashboardExecutiveMetrics
    capacity_and_leakage: DashboardCapacityLeakage
    retention: DashboardRetention
    masters: list[DashboardMasterBreakdownItem]
    services: list[DashboardServiceBreakdownItem]
    booking_funnel: BookingFunnelAggregate
    actionable_signals: list[DashboardActionSignal]
