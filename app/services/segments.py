"""Authoritative segmentation history and a bounded SQL rule compiler.

Visits use completed bookings' end_at, never booking creation or pending status.
Imported last visit supplies recency only. Empty, unclassified history is unknown;
only the explicit imported_is_new_client flag establishes no_visits. Count facts
are observed completed bookings, never an inferred imported count.
"""
from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, BookingServiceItem, BookingStatus
from app.models.campaign_run import CampaignRun
from app.models.customer import Customer
from app.models.messaging import (
    MARKETING_CAMPAIGN_TYPES, Campaign, MessageDeliveryStatus, MessagePurpose, MessageRecipient,
)
from app.schemas.segment import (
    SegmentMember, SegmentPeriod, SegmentPreviewResponse, SegmentRules,
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")
MAX_FACT_BATCH = 500


def evaluation_timestamp(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.utcoffset() is None:
        raise ValueError("evaluated_at must include a timezone offset")
    return value.astimezone(UTC)


def subtract_age(at: datetime, amount: int, unit: str) -> datetime:
    """Months preserve Kyiv wall time and clamp the day; days are fixed 24h."""
    at = evaluation_timestamp(at)
    if unit == "days":
        return at - timedelta(days=amount)
    local = at.astimezone(KYIV_TZ)
    month_index = local.year * 12 + local.month - 1 - amount
    year, zero_month = divmod(month_index, 12)
    if year < 1:
        raise ValueError("Age bound precedes the supported calendar")
    month = zero_month + 1
    shifted = local.replace(year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1]))
    # Resolve DST gaps deterministically by normalizing through UTC. On an
    # ambiguous wall clock use fold=0 (the earlier occurrence).
    return shifted.replace(fold=0).astimezone(UTC)


def period_bounds(period: SegmentPeriod, at: datetime) -> tuple[datetime, datetime]:
    at = evaluation_timestamp(at)
    if period.last is not None:
        return subtract_age(at, period.last, period.unit), at
    return evaluation_timestamp(period.start), min(evaluation_timestamp(period.end), at)


def _in_period(column, period: SegmentPeriod | None, at: datetime):
    if period is None:
        return column <= at
    start, end = period_bounds(period, at)
    return and_(column >= start, column < end)


def upcoming_booking_predicate(evaluated_at: datetime, customer_column=None):
    """Pending and confirmed bookings beginning at or after the evaluation."""
    customer_column = Customer.id if customer_column is None else customer_column
    return select(Booking.id).where(
        Booking.customer_id == customer_column,
        Booking.status.in_((BookingStatus.pending, BookingStatus.confirmed)),
        Booking.start_at >= evaluation_timestamp(evaluated_at),
    ).exists()


def last_visit_at_expression(evaluated_at: datetime):
    """Scalar variant for bounded send-time checks on one customer."""
    at = evaluation_timestamp(evaluated_at)
    local = select(func.max(Booking.end_at)).where(
        Booking.customer_id == Customer.id,
        Booking.status == BookingStatus.completed,
        Booking.end_at <= at,
    ).correlate(Customer).scalar_subquery()
    imported = case((Customer.imported_last_visit_at <= at, Customer.imported_last_visit_at))
    return case((local.is_(None), imported), (imported > local, imported), else_=local)


class SegmentService:
    @staticmethod
    def parse_rules(rules: SegmentRules | dict) -> SegmentRules:
        return rules if isinstance(rules, SegmentRules) else SegmentRules.model_validate(rules)

    @staticmethod
    def last_visit_at_expression(evaluated_at: datetime):
        return last_visit_at_expression(evaluated_at)

    @staticmethod
    def upcoming_booking_predicate(evaluated_at: datetime):
        return upcoming_booking_predicate(evaluated_at)

    def _plan(self, rules: SegmentRules | dict, evaluated_at: datetime, customer_ids=None):
        rules = self.parse_rules(rules)
        at = evaluation_timestamp(evaluated_at)
        # Several independently compiled segment predicates can be OR'ed into
        # one campaign statement, including identical rules. Give each graph a
        # distinct namespace rather than relying on anonymous CTE alias reuse.
        namespace = f"segment_{uuid4().hex}"
        completed_stmt = select(
            Booking.id.label("booking_id"), Booking.customer_id, Booking.master_id,
            Booking.service_id, Booking.end_at,
            func.row_number().over(
                partition_by=Booking.customer_id,
                order_by=(Booking.end_at.desc(), Booking.id.desc()),
            ).label("visit_rank"),
        ).where(
            Booking.status == BookingStatus.completed,
            Booking.end_at <= at,
            Booking.customer_id.is_not(None),
        )
        if customer_ids is not None:
            completed_stmt = completed_stmt.where(Booking.customer_id.in_(customer_ids))
        completed = completed_stmt.cte(f"{namespace}_completed")
        aggregate = select(
            completed.c.customer_id,
            func.max(completed.c.end_at).label("local_last"),
            func.min(completed.c.end_at).label("local_first"),
            func.count().label("visit_count"),
            func.max(case((completed.c.visit_rank == 1, completed.c.master_id))).label("local_last_master"),
        ).group_by(completed.c.customer_id).cte(f"{namespace}_aggregate")
        imported = case((Customer.imported_last_visit_at <= at, Customer.imported_last_visit_at))
        last = case(
            (aggregate.c.local_last.is_(None), imported),
            (imported > aggregate.c.local_last, imported), else_=aggregate.c.local_last,
        )
        # Imported evidence earlier than the first local visit means its true
        # first visit is unavailable. Imported timestamps do not invent masters.
        first = case(
            (imported < aggregate.c.local_first, None), else_=aggregate.c.local_first,
        )
        last_master = case(
            (imported > aggregate.c.local_last, None), else_=aggregate.c.local_last_master,
        )
        history_state = case(
            (last.is_not(None), "known"),
            (and_(Customer.imported_is_new_client.is_(True), Customer.imported_last_visit_at.is_(None)), "no_visits"),
            else_="unknown",
        )
        history_stmt = select(
            Customer.id.label("customer_id"), Customer.name, Customer.phone,
            history_state.label("history_state"), last.label("last_visit_at"),
            func.coalesce(aggregate.c.visit_count, 0).label("completed_visit_count"),
            first.label("first_completed_visit_at"), last_master.label("last_master_id"),
            upcoming_booking_predicate(at).label("has_upcoming_booking"),
        ).outerjoin(aggregate, aggregate.c.customer_id == Customer.id)
        if customer_ids is not None:
            history_stmt = history_stmt.where(Customer.id.in_(customer_ids))
        history = history_stmt.cte(f"{namespace}_history")
        stmt = select(history)
        condition_matches = []
        exclusion_matches = []

        def aggregate_value(source, value, *criteria):
            nonlocal stmt
            grouped = select(source.c.customer_id, value.label("value")).where(*criteria).group_by(
                source.c.customer_id
            ).subquery()
            stmt = stmt.outerjoin(grouped, grouped.c.customer_id == history.c.customer_id)
            return grouped.c.value

        for group, conditions in (("condition", rules.conditions), ("exclusion", rules.exclusions)):
            for index, condition in enumerate(conditions):
                kind = condition.type
                if kind == "last_visit_age":
                    value = history.c.last_visit_at
                    predicates = [value.is_not(None)]
                    if condition.min is not None:
                        cutoff = subtract_age(at, condition.min, condition.unit)
                        predicates.append(value <= cutoff if condition.min_inclusive else value < cutoff)
                    if condition.max is not None:
                        cutoff = subtract_age(at, condition.max, condition.unit)
                        predicates.append(value >= cutoff if condition.max_inclusive else value > cutoff)
                    match = and_(*predicates)
                elif kind == "completed_visit_count":
                    if condition.period is None:
                        value = history.c.completed_visit_count
                    else:
                        value = func.coalesce(aggregate_value(
                            completed, func.count(), _in_period(completed.c.end_at, condition.period, at)
                        ), 0)
                    # Zero observed bookings does not establish zero visits for
                    # an imported-only or unknown customer.
                    predicates = [or_(history.c.completed_visit_count > 0, history.c.history_state == "no_visits")]
                    if condition.min is not None:
                        predicates.append(value >= condition.min)
                    if condition.max is not None:
                        predicates.append(value <= condition.max)
                    match = and_(*predicates)
                elif kind == "upcoming_booking":
                    value = history.c.has_upcoming_booking
                    match = value if condition.present else not_(value)
                elif kind == "visited_master":
                    if condition.mode == "last":
                        value = history.c.last_master_id
                        match = value.in_(condition.master_ids)
                    else:
                        value = aggregate_value(
                            completed, func.max(completed.c.end_at),
                            completed.c.master_id.in_(condition.master_ids),
                            _in_period(completed.c.end_at, condition.period, at),
                        )
                        match = value.is_not(None)
                elif kind == "received_service":
                    has_items = select(BookingServiceItem.id).where(
                        BookingServiceItem.booking_id == completed.c.booking_id,
                    ).exists()
                    has_service = or_(
                        and_(not_(has_items), completed.c.service_id.in_(condition.service_ids)),
                        select(BookingServiceItem.id).where(
                            BookingServiceItem.booking_id == completed.c.booking_id,
                            BookingServiceItem.service_id.in_(condition.service_ids),
                        ).exists(),
                    )
                    value = aggregate_value(completed, func.max(completed.c.end_at), has_service,
                                            _in_period(completed.c.end_at, condition.period, at))
                    match = value.is_not(None)
                elif kind == "first_visit":
                    value = history.c.first_completed_visit_at
                    match = and_(value.is_not(None), _in_period(value, condition.period, at))
                else:
                    # Receipt means provider acceptance (sent or delivered),
                    # never read/open tracking, pending, failed, or skipped.
                    contacts_stmt = select(MessageRecipient.customer_id, MessageRecipient.sent_at).join(
                        Campaign, Campaign.id == MessageRecipient.campaign_id
                    ).outerjoin(
                        CampaignRun, CampaignRun.id == MessageRecipient.run_id
                    ).where(
                        MessageRecipient.status.in_((MessageDeliveryStatus.sent, MessageDeliveryStatus.delivered)),
                        MessageRecipient.sent_at.is_not(None), MessageRecipient.sent_at <= at,
                    )
                    if customer_ids is not None:
                        contacts_stmt = contacts_stmt.where(MessageRecipient.customer_id.in_(customer_ids))
                    if kind == "received_campaign":
                        contacts_stmt = contacts_stmt.where(MessageRecipient.campaign_id == condition.campaign_id)
                    else:
                        contacts_stmt = contacts_stmt.where(or_(
                            and_(MessageRecipient.run_id.is_(None), Campaign.purpose == MessagePurpose.marketing,
                                 Campaign.type.in_(MARKETING_CAMPAIGN_TYPES)),
                            CampaignRun.campaign_snapshot["purpose"].as_string() == MessagePurpose.marketing.value,
                        ))
                    contacts = contacts_stmt.subquery()
                    value = aggregate_value(contacts, func.max(contacts.c.sent_at),
                                            _in_period(contacts.c.sent_at, condition.period, at))
                    match = value.is_not(None)
                    if kind == "marketing_contact" and not condition.present:
                        match = not_(match)
                match = func.coalesce(match, False)
                stmt = stmt.add_columns(match.label(f"{group}_{index}"), value.label(f"{group}_{index}_value"))
                (condition_matches if group == "condition" else exclusion_matches).append(match)
        included = (and_ if rules.combine == "all" else or_)(*condition_matches)
        if exclusion_matches:
            included = and_(included, not_(or_(*exclusion_matches)))
        return stmt, included, rules, at

    def build_predicate(self, rules: SegmentRules | dict, evaluated_at: datetime):
        stmt, included, _, _ = self._plan(rules, evaluated_at)
        matched = stmt.where(included).with_only_columns(stmt.selected_columns.customer_id).cte(
            f"segment_{uuid4().hex}_matched"
        ).prefix_with(
            "MATERIALIZED", dialect="postgresql",
        )
        return Customer.id.in_(select(matched.c.customer_id))

    def build_customer_statement(self, rules: SegmentRules | dict, evaluated_at: datetime, *, customer_ids=None):
        stmt, included, _, _ = self._plan(rules, evaluated_at, customer_ids)
        # PostgreSQL can otherwise push the outer customer into the grouped
        # history query and rescan its CTE once per customer. Materialize the
        # unique matched IDs once (measured 5k-customer regression).
        matched = stmt.where(included).with_only_columns(stmt.selected_columns.customer_id).cte(
            f"segment_{uuid4().hex}_matched"
        ).prefix_with(
            "MATERIALIZED", dialect="postgresql",
        )
        return select(Customer).where(Customer.id.in_(select(matched.c.customer_id)))

    async def member_facts(
        self, session: AsyncSession, customer_ids: Sequence[int], rules: SegmentRules | dict, evaluated_at: datetime,
    ) -> dict[int, dict[str, Any]]:
        if len(customer_ids) > MAX_FACT_BATCH:
            raise ValueError(f"Member facts are limited to {MAX_FACT_BATCH} customers per batch")
        if not customer_ids:
            return {}
        stmt, _, rules, at = self._plan(rules, evaluated_at, customer_ids)
        rows = (await session.execute(stmt)).mappings().all()
        output = {}
        for row in rows:
            facts = {key: row[key] for key in (
                "customer_id", "name", "phone", "history_state", "last_visit_at", "completed_visit_count",
                "first_completed_visit_at", "has_upcoming_booking",
            )}
            for group, conditions in (("condition", rules.conditions), ("exclusion", rules.exclusions)):
                explanations = []
                for index, condition in enumerate(conditions):
                    value = row[f"{group}_{index}_value"]
                    explanation = {
                        "rule": condition.model_dump(mode="json", exclude_none=True),
                        "matched": bool(row[f"{group}_{index}"]),
                        "value": value.isoformat() if isinstance(value, datetime) else value,
                    }
                    period = getattr(condition, "period", None)
                    if period is not None:
                        start, end = period_bounds(period, at)
                        explanation["period_start"] = start.isoformat()
                        explanation["period_end"] = end.isoformat()
                    if condition.type == "last_visit_age":
                        for bound in ("min", "max"):
                            amount = getattr(condition, bound)
                            if amount is not None:
                                explanation[f"{bound}_cutoff"] = subtract_age(at, amount, condition.unit).isoformat()
                    explanations.append(explanation)
                facts[f"{group}s"] = explanations
            output[row["customer_id"]] = facts
        return output

    async def preview(
        self, session: AsyncSession, rules: SegmentRules | dict, *, evaluated_at: datetime | None = None,
        limit: int = 50, offset: int = 0,
    ) -> SegmentPreviewResponse:
        if not 1 <= limit <= 200 or not 0 <= offset <= 1000000:
            raise ValueError("Invalid preview pagination")
        try:
            at = evaluation_timestamp(evaluated_at)
            statement = self.build_customer_statement(rules, at)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid segment evaluation bounds: {exc}") from exc
        total = (await session.execute(select(func.count()).select_from(statement.subquery()))).scalar_one()
        ids = list((await session.execute(
            statement.with_only_columns(Customer.id).order_by(Customer.id).limit(limit).offset(offset)
        )).scalars().all())
        facts = await self.member_facts(session, ids, rules, at)
        return SegmentPreviewResponse(
            evaluated_at=at, total=total, items=[SegmentMember(**facts[customer_id]) for customer_id in ids],
            limit=limit, offset=offset,
        )


segment_service = SegmentService()
