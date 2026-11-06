from datetime import datetime
from typing import Mapping

from sqlalchemy import BigInteger, Numeric, and_, case, cast, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, BookingStatus, Master
from app.schemas.monthly_discounts import (
    DiscountWarning,
    MonthlyDiscountItem,
    MonthlyDiscountPeriod,
    MonthlyDiscountsResponse,
    MonthlyDiscountSummary,
)
from app.services.booking import KYIV_TZ
from app.services.statistics import StatisticsService


def report_statement(start: datetime, end: datetime, *, offset: int, limit: int):
    promo = Booking.promotion_discount_amount
    manual = Booking.manual_discount_amount
    discount = case(
        (and_(promo >= 0, manual >= 0), cast(promo, BigInteger) + cast(manual, BigInteger)),
        else_=None,
    )
    amount_status = case(
        (or_(promo < 0, manual < 0), "invalid"),
        (or_(promo.is_(None), manual.is_(None)), "unknown"),
        else_="known",
    )
    bookings = select(
        Booking.id.label("booking_id"), Booking.start_at, Booking.completed_at,
        Booking.customer_id, Booking.customer_name, Booking.customer_phone,
        Booking.master_id, Master.full_name.label("master_name"),
        cast(Booking.subtotal_amount, Numeric(14, 2)).label("subtotal_amount"),
        cast(promo, Numeric(14, 2)).label("promotion_discount_amount"),
        cast(manual, Numeric(14, 2)).label("manual_discount_amount"),
        cast(discount, Numeric(14, 2)).label("discount_amount"),
        cast(Booking.total_amount, Numeric(14, 2)).label("total_amount"),
        Booking.promotion_id,
        Booking.promotion_code_snapshot.label("promotion_code"),
        Booking.promotion_name_uk_snapshot.label("promotion_name_uk"),
        Booking.promotion_name_en_snapshot.label("promotion_name_en"),
        Booking.promotion_discount_percent_snapshot.label("promotion_discount_percent"),
        Booking.promotion_application_mode_snapshot, Booking.promotion_eligibility_type_snapshot,
        Booking.campaign_offer_id, amount_status.label("amount_status"),
    ).outerjoin(Master, Master.id == Booking.master_id).where(
        Booking.status == BookingStatus.completed,
        Booking.archived_at.is_(None),
        Booking.start_at >= start,
        Booking.start_at < end,
    ).cte("monthly_discount_bookings")
    candidates = select(bookings).where(or_(
        bookings.c.amount_status != "known", bookings.c.discount_amount > 0,
    )).cte("monthly_discount_candidates")
    summary = select(
        func.count().label("completed_bookings"),
        func.count().filter(bookings.c.discount_amount > 0).label("discounted_bookings"),
        func.count().filter(bookings.c.amount_status == "unknown").label("unknown_discount_bookings"),
        func.count().filter(bookings.c.amount_status == "invalid").label("invalid_discount_bookings"),
        cast(func.coalesce(func.sum(bookings.c.discount_amount), 0), Numeric(20, 2)).label("known_discount_amount"),
        select(func.count()).select_from(candidates).scalar_subquery().label("total"),
    ).select_from(bookings).cte("monthly_discount_summary")
    page = select(candidates).order_by(
        candidates.c.start_at.desc(), candidates.c.booking_id.desc(),
    ).offset(offset).limit(limit).cte("monthly_discount_page")
    return select(summary, page).select_from(summary.outerjoin(page, true())).order_by(
        page.c.start_at.desc(), page.c.booking_id.desc(),
    )


def pricing_warnings(row: Mapping) -> list[DiscountWarning]:
    subtotal, total = row["subtotal_amount"], row["total_amount"]
    warnings: list[DiscountWarning] = []
    if subtotal is None or total is None:
        warnings.append("missing_pricing_snapshot")
    if any(value is not None and value < 0 for value in (subtotal, total)):
        warnings.append("invalid_pricing_snapshot")
    discount = row["discount_amount"]
    if discount is not None and subtotal is not None and subtotal >= 0:
        if discount > subtotal:
            warnings.append("discount_exceeds_subtotal")
        if total is not None and total >= 0 and total != max(subtotal - discount, 0):
            warnings.append("snapshot_total_mismatch")
    return warnings


async def get_monthly_discounts(
    session: AsyncSession, *, year: int, month: int, offset: int = 0, limit: int = 50,
) -> MonthlyDiscountsResponse:
    start, end = StatisticsService().month_bounds(year, month)
    result = await session.execute(report_statement(start, end, offset=offset, limit=limit))
    rows = result.mappings().all()
    return MonthlyDiscountsResponse(
        period=MonthlyDiscountPeriod(year=year, month=month),
        generated_at=datetime.now(KYIV_TZ),
        summary=MonthlyDiscountSummary.model_validate(rows[0]),
        items=[
            MonthlyDiscountItem.model_validate({**row, "warnings": pricing_warnings(row)})
            for row in rows if row["booking_id"] is not None
        ],
        total=rows[0]["total"], offset=offset, limit=limit,
    )
