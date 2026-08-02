from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import String, cast, distinct, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import BarberService, Master
from app.models.booking_funnel import (
    BookingFunnelEvent,
    BookingFunnelEventSource,
    BookingFunnelEventType,
    BookingFunnelWeeklyDigest,
)
from app.schemas.booking_funnel import (
    FUNNEL_STEP_TYPES,
    BookingFunnelAggregate,
    BookingFunnelAlertThresholds,
    BookingFunnelConversionMetric,
    BookingFunnelDropOffMetric,
    BookingFunnelNoSlotContextMetric,
    BookingFunnelNoSlotDateMetric,
    BookingFunnelNoSlotServiceRef,
    BookingFunnelOperationalAlert,
    BookingFunnelOverallConversion,
    BookingFunnelRecommendedAction,
    BookingFunnelStepMetric,
    BookingFunnelWeeklyDigestResponse,
    PublicBookingFunnelEventCreate,
)

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")
RATE_QUANT = Decimal("0.01")
_DIGEST_LOCK_ID = 1_904_261_718
NO_SLOT_CONTEXT_LIMIT = 250


@dataclass(frozen=True)
class BookingFunnelThresholdConfig:
    no_slot_min_count: int
    no_slot_rate_percent: Decimal
    stale_schedule_count: int
    booking_error_count: int
    meaningful_step_sessions: int

    @classmethod
    def from_settings(cls) -> "BookingFunnelThresholdConfig":
        return cls(
            no_slot_min_count=settings.booking_funnel_no_slot_alert_min_count,
            no_slot_rate_percent=Decimal(str(settings.booking_funnel_no_slot_alert_rate_percent)).quantize(
                RATE_QUANT
            ),
            stale_schedule_count=settings.booking_funnel_stale_schedule_alert_count,
            booking_error_count=settings.booking_funnel_error_alert_count,
            meaningful_step_sessions=settings.booking_funnel_meaningful_step_sessions,
        )


@dataclass(frozen=True)
class WeeklyDigestResult:
    digest: BookingFunnelWeeklyDigest
    created: bool


def _percent(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return (
        Decimal(numerator) * Decimal("100") / Decimal(denominator)
    ).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def _threshold_score(
    value: int | Decimal,
    threshold: int | Decimal,
) -> Decimal:
    normalized_threshold = Decimal(str(threshold))
    if normalized_threshold <= 0:
        return Decimal("100.00")
    return (
        Decimal(str(value)) * Decimal("100") / normalized_threshold
    ).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def _hash_identifier(purpose: str, value: str) -> str:
    key = (settings.booking_funnel_hash_secret or settings.secret_key).encode("utf-8")
    material = f"{purpose}:{value}".encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _hash_session(anonymous_session_id: str) -> str:
    return _hash_identifier("session", anonymous_session_id)


def _action(
    code: str,
    *,
    explanation: str,
    route: str,
    based_on: str,
) -> BookingFunnelRecommendedAction:
    titles = {
        "review_availability": "Переглянути доступність майстрів",
        "refresh_schedule": "Оновити опублікований розклад",
        "investigate_booking_errors": "Перевірити помилки запису",
        "improve_service_discovery": "Спростити вибір послуги",
        "clarify_master_choice": "Уточнити вибір майстра",
        "simplify_contact_step": "Спростити введення контактів",
        "investigate_booking_completion": "Перевірити завершення запису",
    }
    return BookingFunnelRecommendedAction(
        code=code,  # type: ignore[arg-type]
        title_uk=titles[code],
        explanation_uk=explanation,
        recommended_backoffice_route=route,
        based_on=based_on,
    )


def _recommend_action(
    conversions: list[BookingFunnelConversionMetric],
    alerts: list[BookingFunnelOperationalAlert],
    thresholds: BookingFunnelThresholdConfig,
) -> BookingFunnelRecommendedAction | None:
    candidates: list[tuple[Decimal, int, BookingFunnelRecommendedAction]] = []
    alert_by_code = {item.code: item for item in alerts}

    booking_error = alert_by_code["booking_error"]
    if booking_error.triggered:
        score = _threshold_score(
            booking_error.count,
            thresholds.booking_error_count,
        )
        candidates.append(
            (
                score,
                0,
                _action(
                    "investigate_booking_errors",
                    explanation=(
                        f"Зафіксовано {booking_error.count} помилок у процесі запису; "
                        "перевірте журнали API та останні зміни форми."
                    ),
                    route="/bookings",
                    based_on="booking_error",
                ),
            )
        )

    no_slot = alert_by_code["no_slot"]
    if no_slot.triggered and no_slot.rate_percent is not None:
        count_score = _threshold_score(
            no_slot.count,
            thresholds.no_slot_min_count,
        )
        rate_score = (
            _threshold_score(
                no_slot.rate_percent,
                thresholds.no_slot_rate_percent,
            )
            if thresholds.no_slot_rate_percent > 0
            else count_score
        )
        candidates.append(
            (
                min(count_score, rate_score),
                1,
                _action(
                    "review_availability",
                    explanation=(
                        f"{no_slot.rate_percent}% сесій після вибору майстра повідомили "
                        "про відсутність слотів; перевірте опубліковану доступність."
                    ),
                    route="/time-blocks",
                    based_on="no_slot",
                ),
            )
        )

    stale_schedule = alert_by_code["stale_schedule"]
    if stale_schedule.triggered:
        score = _threshold_score(
            stale_schedule.count,
            thresholds.stale_schedule_count,
        )
        candidates.append(
            (
                score,
                2,
                _action(
                    "refresh_schedule",
                    explanation=(
                        f"Зафіксовано {stale_schedule.count} випадків застарілого розкладу; "
                        "звірте вікна доступності з фактичним календарем."
                    ),
                    route="/time-blocks",
                    based_on="stale_schedule",
                ),
            )
        )

    transition_actions = {
        (
            BookingFunnelEventType.booking_start,
            BookingFunnelEventType.service_selected,
        ): (
            "improve_service_discovery",
            "Спростіть назви, групування та пояснення послуг у першому кроці.",
            "/services",
        ),
        (
            BookingFunnelEventType.service_selected,
            BookingFunnelEventType.master_selected,
        ): (
            "clarify_master_choice",
            "Додайте чіткіші описи майстрів і підказку, як обрати спеціаліста.",
            "/masters",
        ),
        (
            BookingFunnelEventType.master_selected,
            BookingFunnelEventType.slot_selected,
        ): (
            "review_availability",
            "Перевірте кількість і розподіл доступних слотів після вибору майстра.",
            "/time-blocks",
        ),
        (
            BookingFunnelEventType.slot_selected,
            BookingFunnelEventType.contact_entered,
        ): (
            "simplify_contact_step",
            "Скоротіть контактну форму та поясніть, навіщо потрібні обов’язкові поля.",
            "/bookings",
        ),
        (
            BookingFunnelEventType.contact_entered,
            BookingFunnelEventType.booking_success,
        ): (
            "investigate_booking_completion",
            "Перевірте валідацію, конфлікти слотів і повідомлення про помилки на фінальному кроці.",
            "/bookings",
        ),
    }
    for index, conversion in enumerate(conversions):
        if (
            conversion.status != "available"
            or conversion.from_count < thresholds.meaningful_step_sessions
            or conversion.conversion_percent is None
        ):
            continue
        drop_rate = (Decimal("100.00") - conversion.conversion_percent).quantize(RATE_QUANT)
        if drop_rate <= 0:
            continue
        code, explanation, route = transition_actions[(conversion.from_step, conversion.to_step)]
        candidates.append(
            (
                drop_rate,
                10 + index,
                _action(
                    code,
                    explanation=explanation,
                    route=route,
                    based_on=f"{conversion.from_step.value}_to_{conversion.to_step.value}",
                ),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1], item[2].code))
    return candidates[0][2]


def build_funnel_aggregate(
    counts: dict[BookingFunnelEventType, int],
    *,
    unattributed_booking_successes: int,
    thresholds: BookingFunnelThresholdConfig,
    transition_counts: dict[
        tuple[BookingFunnelEventType, BookingFunnelEventType],
        int,
    ] | None = None,
    overall_success_sessions: int | None = None,
    tracking_gaps: dict[
        tuple[BookingFunnelEventType, BookingFunnelEventType],
        int,
    ] | None = None,
    no_slot_rate_sessions: int | None = None,
    no_slot_denominator_sessions: int | None = None,
    no_slot_dates: list[BookingFunnelNoSlotDateMetric] | None = None,
    no_slot_contexts: list[BookingFunnelNoSlotContextMetric] | None = None,
    no_slot_contexts_truncated: bool = False,
    no_slot_unknown_date_count: int = 0,
    latest_digest: BookingFunnelWeeklyDigestResponse | None = None,
) -> BookingFunnelAggregate:
    normalized = {event_type: int(counts.get(event_type, 0)) for event_type in BookingFunnelEventType}
    normalized_no_slot_dates = no_slot_dates or []
    normalized_no_slot_contexts = no_slot_contexts or []
    total = sum(normalized.values())
    threshold_response = BookingFunnelAlertThresholds(
        no_slot_min_count=thresholds.no_slot_min_count,
        no_slot_rate_percent=thresholds.no_slot_rate_percent,
        stale_schedule_count=thresholds.stale_schedule_count,
        booking_error_count=thresholds.booking_error_count,
        meaningful_step_sessions=thresholds.meaningful_step_sessions,
    )
    if total == 0 and unattributed_booking_successes == 0:
        return BookingFunnelAggregate(
            status="empty",
            status_reason="No booking funnel events were recorded in the selected period.",
            tracking_gap_count=0,
            steps=[],
            step_to_step_conversion=[],
            overall_conversion=None,
            drop_offs=[],
            operational_alerts=[],
            alert_thresholds=threshold_response,
            no_slot_dates=normalized_no_slot_dates,
            no_slot_contexts=normalized_no_slot_contexts,
            no_slot_context_limit=NO_SLOT_CONTEXT_LIMIT,
            no_slot_contexts_truncated=no_slot_contexts_truncated,
            no_slot_unknown_date_count=no_slot_unknown_date_count,
            unattributed_booking_successes=0,
            weekly_insight_uk="За вибраний період подій воронки ще немає.",
            recommended_action=None,
            latest_weekly_digest=latest_digest,
        )

    steps = [
        BookingFunnelStepMetric(event_type=event_type, count=normalized[event_type])
        for event_type in FUNNEL_STEP_TYPES
    ]
    conversions: list[BookingFunnelConversionMetric] = []
    drop_offs: list[BookingFunnelDropOffMetric] = []
    data_anomaly = False
    normalized_tracking_gaps: dict[
        tuple[BookingFunnelEventType, BookingFunnelEventType],
        int,
    ] = {}
    for from_step, to_step in zip(FUNNEL_STEP_TYPES, FUNNEL_STEP_TYPES[1:]):
        transition = (from_step, to_step)
        from_count = normalized[from_step]
        destination_count = normalized[to_step]
        continued_count = (
            int(transition_counts.get(transition, 0))
            if transition_counts is not None
            else min(from_count, destination_count)
        )
        gap_count = (
            int(tracking_gaps.get(transition, 0))
            if tracking_gaps is not None
            else max(0, destination_count - from_count)
        )
        normalized_tracking_gaps[transition] = max(0, gap_count)
        if gap_count > 0:
            data_anomaly = True
        if from_count <= 0:
            conversion = BookingFunnelConversionMetric(
                from_step=from_step,
                to_step=to_step,
                from_count=from_count,
                to_count=0,
                conversion_percent=None,
                status="unavailable",
                unavailable_reason="The preceding step has no recorded sessions.",
            )
        elif continued_count < 0 or continued_count > from_count:
            data_anomaly = True
            conversion = BookingFunnelConversionMetric(
                from_step=from_step,
                to_step=to_step,
                from_count=from_count,
                to_count=max(0, continued_count),
                conversion_percent=None,
                status="unavailable",
                unavailable_reason="The same-session transition count is outside its valid denominator.",
            )
        else:
            conversion = BookingFunnelConversionMetric(
                from_step=from_step,
                to_step=to_step,
                from_count=from_count,
                to_count=continued_count,
                conversion_percent=_percent(continued_count, from_count),
                status="available",
            )
        conversions.append(conversion)
        if conversion.status == "available" and conversion.conversion_percent is not None:
            drop_offs.append(
                BookingFunnelDropOffMetric(
                    from_step=from_step,
                    to_step=to_step,
                    count=from_count - continued_count,
                    drop_off_percent=(Decimal("100.00") - conversion.conversion_percent).quantize(RATE_QUANT),
                    status="available",
                )
            )
        else:
            drop_offs.append(
                BookingFunnelDropOffMetric(
                    from_step=from_step,
                    to_step=to_step,
                    count=None,
                    drop_off_percent=None,
                    status="unavailable",
                )
            )

    starts = normalized[BookingFunnelEventType.booking_start]
    attributed_successes = normalized[BookingFunnelEventType.booking_success]
    successes = (
        int(overall_success_sessions)
        if overall_success_sessions is not None
        else min(starts, attributed_successes)
    )
    if starts <= 0:
        overall = BookingFunnelOverallConversion(
            started=starts,
            succeeded=successes,
            conversion_percent=None,
            status="unavailable",
            unavailable_reason="No booking_start baseline was recorded.",
        )
    elif successes < 0 or successes > starts:
        data_anomaly = True
        overall = BookingFunnelOverallConversion(
            started=starts,
            succeeded=successes,
            conversion_percent=None,
            status="unavailable",
            unavailable_reason="The same-session success count is outside its valid start cohort.",
        )
    else:
        overall = BookingFunnelOverallConversion(
            started=starts,
            succeeded=successes,
            conversion_percent=_percent(successes, starts),
            status="available",
        )

    master_selected = (
        int(no_slot_denominator_sessions)
        if no_slot_denominator_sessions is not None
        else normalized[BookingFunnelEventType.master_selected]
    )
    no_slot_sessions = (
        int(no_slot_rate_sessions)
        if no_slot_rate_sessions is not None
        else min(normalized[BookingFunnelEventType.no_slot], master_selected)
    )
    no_slot_rate = _percent(no_slot_sessions, master_selected)
    alerts = [
        BookingFunnelOperationalAlert(
            code="no_slot",
            count=normalized[BookingFunnelEventType.no_slot],
            rate_percent=no_slot_rate,
            triggered=(
                normalized[BookingFunnelEventType.no_slot] >= thresholds.no_slot_min_count
                and no_slot_rate is not None
                and no_slot_rate >= thresholds.no_slot_rate_percent
            ),
        ),
        BookingFunnelOperationalAlert(
            code="stale_schedule",
            count=normalized[BookingFunnelEventType.stale_schedule],
            rate_percent=None,
            triggered=(
                normalized[BookingFunnelEventType.stale_schedule] >= thresholds.stale_schedule_count
            ),
        ),
        BookingFunnelOperationalAlert(
            code="booking_error",
            count=normalized[BookingFunnelEventType.booking_error],
            rate_percent=None,
            triggered=(normalized[BookingFunnelEventType.booking_error] >= thresholds.booking_error_count),
        ),
    ]
    recommendation = _recommend_action(conversions, alerts, thresholds)
    available_drop_offs = [item for item in drop_offs if item.count is not None]
    largest_drop = max(
        available_drop_offs,
        key=lambda item: (item.count or 0, -(FUNNEL_STEP_TYPES.index(item.from_step))),
        default=None,
    )
    if overall.status == "available":
        weekly_insight = (
            f"Із {starts} сесій, що почали запис, {successes} завершили його "
            f"({overall.conversion_percent}%)."
        )
        if largest_drop is not None and largest_drop.count:
            weekly_insight += (
                f" Найбільша втрата — між {largest_drop.from_step.value} і "
                f"{largest_drop.to_step.value}: {largest_drop.count} сесій."
            )
    else:
        weekly_insight = (
            "Події воронки є, але повну конверсію неможливо надійно розрахувати через "
            "неповну базову або послідовну телеметрію."
        )

    tracking_gap_count = sum(normalized_tracking_gaps.values())
    if starts == 0:
        aggregate_status = "unavailable"
        status_reason = "Events exist, but booking_start was not recorded."
    elif data_anomaly or unattributed_booking_successes:
        aggregate_status = "partial"
        reasons = []
        if unattributed_booking_successes:
            reasons.append(
                f"{unattributed_booking_successes} server-side booking successes have no anonymous session"
            )
        if tracking_gap_count:
            reasons.append(
                f"{tracking_gap_count} destination session-transition pairs lack the preceding event"
            )
        if not reasons:
            reasons.append("one or more same-session transition counts are invalid")
        status_reason = "; ".join(reasons) + "."
    else:
        aggregate_status = "available"
        status_reason = None

    return BookingFunnelAggregate(
        status=aggregate_status,
        status_reason=status_reason,
        tracking_gap_count=tracking_gap_count,
        steps=steps,
        step_to_step_conversion=conversions,
        overall_conversion=overall,
        drop_offs=drop_offs,
        operational_alerts=alerts,
        alert_thresholds=threshold_response,
        no_slot_dates=normalized_no_slot_dates,
        no_slot_contexts=normalized_no_slot_contexts,
        no_slot_context_limit=NO_SLOT_CONTEXT_LIMIT,
        no_slot_contexts_truncated=no_slot_contexts_truncated,
        no_slot_unknown_date_count=no_slot_unknown_date_count,
        unattributed_booking_successes=unattributed_booking_successes,
        weekly_insight_uk=weekly_insight,
        recommended_action=recommendation,
        latest_weekly_digest=latest_digest,
    )


class BookingFunnelService:
    def __init__(self, thresholds: BookingFunnelThresholdConfig | None = None) -> None:
        self.thresholds = thresholds or BookingFunnelThresholdConfig.from_settings()

    async def record_public_event(
        self,
        session: AsyncSession,
        payload: PublicBookingFunnelEventCreate,
    ) -> bool:
        service_ids = (
            payload.service_ids
            if payload.service_ids is not None
            else (
                [payload.service_id]
                if payload.event_type == BookingFunnelEventType.no_slot
                and payload.service_id
                else []
            )
        )
        if payload.service_ids is not None:
            existing_service_ids = set(
                (
                    await session.execute(
                        select(BarberService.id).where(
                            BarberService.id.in_(service_ids),
                            BarberService.master_id == payload.master_id,
                        )
                    )
                ).scalars().all()
            )
            if existing_service_ids != set(service_ids):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="service_ids must reference services of the selected master",
                )
        values = {
            "event_id_hash": _hash_identifier("event", payload.event_id),
            "event_type": payload.event_type,
            "source": BookingFunnelEventSource.client,
            "anonymous_session_hash": _hash_session(payload.anonymous_session_id),
            "master_id": payload.master_id,
            "service_id": payload.service_id,
            "booking_id": None,
            "target_date": payload.target_date,
            "service_ids_key": ",".join(str(service_id) for service_id in service_ids) or None,
            "occurred_at": datetime.now(KYIV_TZ),
        }
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "sqlite":
            statement = sqlite_insert(BookingFunnelEvent).values(**values).on_conflict_do_nothing()
        else:
            statement = postgresql_insert(BookingFunnelEvent).values(**values).on_conflict_do_nothing()
        statement = statement.returning(BookingFunnelEvent.id)
        try:
            inserted_id = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="master_id or service_id does not reference a current booking option",
            ) from exc
        except Exception:
            await session.rollback()
            raise
        logger.info(
            "Booking funnel client event processed event_type=%s recorded=%s",
            payload.event_type.value,
            inserted_id is not None,
        )
        return inserted_id is not None

    def add_booking_success(
        self,
        session: AsyncSession,
        *,
        booking_id: int,
        master_id: int,
        service_id: int,
        anonymous_session_id: str | None,
        occurred_at: datetime | None = None,
    ) -> BookingFunnelEvent:
        observed_at = occurred_at or datetime.now(KYIV_TZ)
        anonymous_session_hash = (
            _hash_session(anonymous_session_id)
            if anonymous_session_id is not None
            else None
        )
        event = BookingFunnelEvent(
            event_id_hash=_hash_identifier("server_event", f"booking:{booking_id}"),
            event_type=BookingFunnelEventType.booking_success,
            source=BookingFunnelEventSource.server,
            anonymous_session_hash=anonymous_session_hash,
            master_id=master_id,
            service_id=service_id,
            booking_id=booking_id,
            target_date=None,
            service_ids_key=None,
            occurred_at=observed_at,
        )
        session.add(event)
        if anonymous_session_hash is not None:
            for event_type in FUNNEL_STEP_TYPES[:-1]:
                session.add(
                    BookingFunnelEvent(
                        event_id_hash=_hash_identifier(
                            "server_event",
                            f"booking:{booking_id}:{event_type.value}",
                        ),
                        event_type=event_type,
                        source=BookingFunnelEventSource.server,
                        anonymous_session_hash=anonymous_session_hash,
                        master_id=master_id,
                        service_id=service_id,
                        booking_id=None,
                        target_date=None,
                        service_ids_key=None,
                        occurred_at=observed_at,
                    )
                )
        return event

    async def aggregate(
        self,
        session: AsyncSession,
        *,
        start: datetime,
        end: datetime,
        master_id: int | None = None,
        include_latest_digest: bool = True,
    ) -> BookingFunnelAggregate:
        cohort_sessions = (
            select(BookingFunnelEvent.anonymous_session_hash)
            .where(
                BookingFunnelEvent.event_type == BookingFunnelEventType.booking_start,
                BookingFunnelEvent.anonymous_session_hash.is_not(None),
            )
            .group_by(BookingFunnelEvent.anonymous_session_hash)
            .having(
                func.min(BookingFunnelEvent.occurred_at) >= start,
                func.min(BookingFunnelEvent.occurred_at) < end,
            )
        )
        statement = (
            select(
                BookingFunnelEvent.event_type,
                BookingFunnelEvent.anonymous_session_hash,
                BookingFunnelEvent.master_id,
                BookingFunnelEvent.booking_id,
            )
            .where(
                BookingFunnelEvent.anonymous_session_hash.in_(cohort_sessions),
            )
        )
        rows = (await session.execute(statement)).all()

        period_event_types = (
            BookingFunnelEventType.master_selected,
            BookingFunnelEventType.booking_success,
            BookingFunnelEventType.no_slot,
            BookingFunnelEventType.stale_schedule,
            BookingFunnelEventType.booking_error,
        )
        period_statement = select(
            BookingFunnelEvent.event_type,
            BookingFunnelEvent.anonymous_session_hash,
            BookingFunnelEvent.master_id,
            BookingFunnelEvent.id,
        ).where(
            BookingFunnelEvent.event_type.in_(period_event_types),
            BookingFunnelEvent.occurred_at >= start,
            BookingFunnelEvent.occurred_at < end,
        )
        period_rows = (await session.execute(period_statement)).all()

        no_slot_statement = (
            select(
                BookingFunnelEvent.target_date,
                func.count(),
                func.count(distinct(BookingFunnelEvent.anonymous_session_hash)),
                func.count(distinct(BookingFunnelEvent.master_id)),
                func.min(BookingFunnelEvent.occurred_at),
                func.max(BookingFunnelEvent.occurred_at),
            )
            .where(
                BookingFunnelEvent.event_type == BookingFunnelEventType.no_slot,
                BookingFunnelEvent.occurred_at >= start,
                BookingFunnelEvent.occurred_at < end,
            )
            .group_by(BookingFunnelEvent.target_date)
            .order_by(BookingFunnelEvent.target_date.asc().nulls_last())
        )
        if master_id is not None:
            no_slot_statement = no_slot_statement.where(BookingFunnelEvent.master_id == master_id)
        no_slot_rows = (await session.execute(no_slot_statement)).all()

        context_observations = func.count().label("observations")
        context_service_ids_key = func.coalesce(
            BookingFunnelEvent.service_ids_key,
            cast(BookingFunnelEvent.service_id, String),
        ).label("context_service_ids_key")
        no_slot_context_statement = (
            select(
                BookingFunnelEvent.target_date,
                BookingFunnelEvent.master_id,
                Master.full_name,
                Master.last_name,
                context_service_ids_key,
                context_observations,
                func.count(distinct(BookingFunnelEvent.anonymous_session_hash)),
                func.min(BookingFunnelEvent.occurred_at),
                func.max(BookingFunnelEvent.occurred_at),
            )
            .outerjoin(Master, Master.id == BookingFunnelEvent.master_id)
            .where(
                BookingFunnelEvent.event_type == BookingFunnelEventType.no_slot,
                BookingFunnelEvent.target_date.is_not(None),
                BookingFunnelEvent.occurred_at >= start,
                BookingFunnelEvent.occurred_at < end,
            )
            .group_by(
                BookingFunnelEvent.target_date,
                BookingFunnelEvent.master_id,
                Master.full_name,
                Master.last_name,
                context_service_ids_key,
            )
            .order_by(
                BookingFunnelEvent.target_date.desc(),
                context_observations.desc(),
                BookingFunnelEvent.master_id.asc().nulls_last(),
                context_service_ids_key.asc().nulls_last(),
            )
            .limit(NO_SLOT_CONTEXT_LIMIT + 1)
        )
        if master_id is not None:
            no_slot_context_statement = no_slot_context_statement.where(
                BookingFunnelEvent.master_id == master_id
            )
        raw_no_slot_context_rows = (await session.execute(no_slot_context_statement)).all()
        no_slot_contexts_truncated = len(raw_no_slot_context_rows) > NO_SLOT_CONTEXT_LIMIT
        no_slot_context_rows = raw_no_slot_context_rows[:NO_SLOT_CONTEXT_LIMIT]

        context_service_ids: set[int] = set()
        parsed_context_service_ids: list[list[int]] = []
        for _, _, _, _, service_ids_key, *_ in no_slot_context_rows:
            parsed_ids = []
            if service_ids_key:
                parsed_ids = [
                    int(value)
                    for value in str(service_ids_key).split(",")
                    if value.isdigit() and int(value) > 0
                ]
            parsed_context_service_ids.append(parsed_ids)
            context_service_ids.update(parsed_ids)

        service_names: dict[int, str] = {}
        if context_service_ids:
            service_name_rows = (
                await session.execute(
                    select(
                        BarberService.id,
                        func.coalesce(BarberService.title_uk, BarberService.name),
                    ).where(BarberService.id.in_(context_service_ids))
                )
            ).all()
            service_names = {
                int(service_id): str(service_name)
                for service_id, service_name in service_name_rows
            }

        step_sessions = {
            event_type: set()
            for event_type in FUNNEL_STEP_TYPES
        }
        scoped_sessions = (
            {
                anonymous_session_hash
                for _, anonymous_session_hash, row_master_id, _ in rows
                if anonymous_session_hash is not None
                and row_master_id == master_id
            }
            if master_id is not None
            else set()
        )
        early_attributable_steps = {
            BookingFunnelEventType.booking_start,
            BookingFunnelEventType.service_selected,
        }
        for event_type, anonymous_session_hash, row_master_id, _ in rows:
            normalized_type = (
                event_type
                if isinstance(event_type, BookingFunnelEventType)
                else BookingFunnelEventType(str(event_type))
            )
            if (
                normalized_type not in step_sessions
                or anonymous_session_hash is None
            ):
                continue
            if master_id is None:
                step_sessions[normalized_type].add(anonymous_session_hash)
                continue
            if row_master_id == master_id:
                step_sessions[normalized_type].add(anonymous_session_hash)
                continue
            if (
                normalized_type in early_attributable_steps
                and row_master_id is None
                and anonymous_session_hash in scoped_sessions
            ):
                step_sessions[normalized_type].add(anonymous_session_hash)

        operational_types = {
            BookingFunnelEventType.master_selected,
            BookingFunnelEventType.no_slot,
            BookingFunnelEventType.stale_schedule,
            BookingFunnelEventType.booking_error,
        }
        operational_sessions = {
            event_type: set()
            for event_type in operational_types
        }
        unattributed_event_ids: set[int] = set()
        for event_type, anonymous_session_hash, row_master_id, event_row_id in period_rows:
            normalized_type = (
                event_type
                if isinstance(event_type, BookingFunnelEventType)
                else BookingFunnelEventType(str(event_type))
            )
            if master_id is not None and row_master_id != master_id:
                continue
            if (
                normalized_type == BookingFunnelEventType.booking_success
                and anonymous_session_hash is None
            ):
                unattributed_event_ids.add(int(event_row_id))
            if (
                normalized_type in operational_sessions
                and anonymous_session_hash is not None
            ):
                operational_sessions[normalized_type].add(anonymous_session_hash)

        counts = {event_type: 0 for event_type in BookingFunnelEventType}
        for event_type, sessions in step_sessions.items():
            counts[event_type] = len(sessions)
        for event_type in (
            BookingFunnelEventType.no_slot,
            BookingFunnelEventType.stale_schedule,
            BookingFunnelEventType.booking_error,
        ):
            counts[event_type] = len(operational_sessions[event_type])

        transition_counts = {
            (from_step, to_step): len(
                step_sessions[from_step] & step_sessions[to_step]
            )
            for from_step, to_step in zip(
                FUNNEL_STEP_TYPES,
                FUNNEL_STEP_TYPES[1:],
            )
        }
        tracking_gaps = {
            (from_step, to_step): len(
                step_sessions[to_step] - step_sessions[from_step]
            )
            for from_step, to_step in zip(
                FUNNEL_STEP_TYPES,
                FUNNEL_STEP_TYPES[1:],
            )
        }
        overall_success_sessions = len(
            step_sessions[BookingFunnelEventType.booking_start]
            & step_sessions[BookingFunnelEventType.booking_success]
        )
        no_slot_rate_sessions = len(
            operational_sessions[BookingFunnelEventType.master_selected]
            & operational_sessions[BookingFunnelEventType.no_slot]
        )
        no_slot_denominator_sessions = len(
            operational_sessions[BookingFunnelEventType.master_selected]
        )
        no_slot_dates = []
        no_slot_unknown_date_count = 0
        for (
            target_date,
            observations,
            unique_sessions,
            affected_masters,
            first_observed_at,
            last_observed_at,
        ) in no_slot_rows:
            if target_date is None:
                no_slot_unknown_date_count = int(observations or 0)
                continue
            no_slot_dates.append(
                BookingFunnelNoSlotDateMetric(
                    target_date=target_date,
                    observations=int(observations or 0),
                    unique_sessions=int(unique_sessions or 0),
                    affected_masters=int(affected_masters or 0),
                    first_observed_at=first_observed_at,
                    last_observed_at=last_observed_at,
                )
            )
        no_slot_contexts = []
        for row, service_ids in zip(no_slot_context_rows, parsed_context_service_ids):
            (
                target_date,
                context_master_id,
                master_first_name,
                master_last_name,
                _,
                observations,
                unique_sessions,
                first_observed_at,
                last_observed_at,
            ) = row
            master_name = " ".join(
                value for value in (master_first_name, master_last_name) if value
            ) or None
            no_slot_contexts.append(
                BookingFunnelNoSlotContextMetric(
                    target_date=target_date,
                    master_id=context_master_id,
                    master_name=master_name,
                    services=[
                        BookingFunnelNoSlotServiceRef(
                            service_id=service_id,
                            service_name=service_names.get(service_id),
                        )
                        for service_id in service_ids
                    ],
                    observations=int(observations or 0),
                    unique_sessions=int(unique_sessions or 0),
                    first_observed_at=first_observed_at,
                    last_observed_at=last_observed_at,
                )
            )
        latest = await self.latest_digest(session) if include_latest_digest else None
        return build_funnel_aggregate(
            counts,
            unattributed_booking_successes=len(unattributed_event_ids),
            thresholds=self.thresholds,
            transition_counts=transition_counts,
            overall_success_sessions=overall_success_sessions,
            tracking_gaps=tracking_gaps,
            no_slot_rate_sessions=no_slot_rate_sessions,
            no_slot_denominator_sessions=no_slot_denominator_sessions,
            no_slot_dates=no_slot_dates,
            no_slot_contexts=no_slot_contexts,
            no_slot_contexts_truncated=no_slot_contexts_truncated,
            no_slot_unknown_date_count=no_slot_unknown_date_count,
            latest_digest=latest,
        )

    async def latest_digest(
        self,
        session: AsyncSession,
    ) -> BookingFunnelWeeklyDigestResponse | None:
        digest = (
            await session.execute(
                select(BookingFunnelWeeklyDigest)
                .order_by(
                    BookingFunnelWeeklyDigest.period_end.desc(),
                    BookingFunnelWeeklyDigest.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if digest is None:
            return None
        payload = digest.payload_json or {}
        if payload.get("calculation_version") != 2:
            return None
        recommended_payload = payload.get("recommended_action")
        return BookingFunnelWeeklyDigestResponse(
            period_start=digest.period_start,
            period_end=digest.period_end,
            generated_at=digest.generated_at,
            status=digest.data_status,
            insight_uk=digest.insight_uk,
            recommended_action=(
                BookingFunnelRecommendedAction.model_validate(recommended_payload)
                if recommended_payload
                else None
            ),
            step_counts=[
                BookingFunnelStepMetric.model_validate(item)
                for item in payload.get("step_counts", [])
            ],
            operational_alerts=[
                BookingFunnelOperationalAlert.model_validate(item)
                for item in payload.get("operational_alerts", [])
            ],
        )

    async def _try_digest_lock(self, session: AsyncSession) -> bool:
        if session.get_bind().dialect.name != "postgresql":
            return True
        return bool(
            (
                await session.execute(
                    select(func.pg_try_advisory_xact_lock(_DIGEST_LOCK_ID))
                )
            ).scalar_one()
        )

    async def generate_latest_completed_week(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> WeeklyDigestResult | None:
        now_kyiv = (now or datetime.now(KYIV_TZ)).astimezone(KYIV_TZ)
        current_week_start = now_kyiv.date() - timedelta(days=now_kyiv.weekday())
        period_end = current_week_start - timedelta(days=1)
        period_start = period_end - timedelta(days=6)
        if not await self._try_digest_lock(session):
            await session.rollback()
            logger.info(
                "Booking funnel weekly digest skipped reason=lock_not_acquired period_start=%s period_end=%s",
                period_start,
                period_end,
            )
            return None
        existing = (
            await session.execute(
                select(BookingFunnelWeeklyDigest).where(
                    BookingFunnelWeeklyDigest.period_start == period_start,
                    BookingFunnelWeeklyDigest.period_end == period_end,
                )
            )
        ).scalar_one_or_none()
        start = datetime.combine(period_start, time.min, tzinfo=KYIV_TZ)
        end = datetime.combine(period_end + timedelta(days=1), time.min, tzinfo=KYIV_TZ)
        aggregate = await self.aggregate(
            session,
            start=start,
            end=end,
            include_latest_digest=False,
        )
        generated_at = now_kyiv
        digest = existing or BookingFunnelWeeklyDigest(
            period_start=period_start,
            period_end=period_end,
        )
        digest.generated_at = generated_at
        digest.data_status = aggregate.status
        digest.insight_uk = aggregate.weekly_insight_uk
        digest.recommended_action_code = (
            aggregate.recommended_action.code if aggregate.recommended_action else None
        )
        digest.recommended_action_uk = (
            aggregate.recommended_action.explanation_uk
            if aggregate.recommended_action
            else None
        )
        digest.payload_json = {
            "calculation_version": 2,
            "recommended_action": (
                aggregate.recommended_action.model_dump(mode="json")
                if aggregate.recommended_action
                else None
            ),
            "step_counts": [item.model_dump(mode="json") for item in aggregate.steps],
            "operational_alerts": [
                item.model_dump(mode="json") for item in aggregate.operational_alerts
            ],
        }
        if existing is None:
            session.add(digest)
        try:
            await session.commit()
            await session.refresh(digest)
        except Exception:
            await session.rollback()
            raise
        logger.info(
            "Booking funnel weekly digest stored period_start=%s period_end=%s digest_id=%s status=%s recalculated=%s",
            period_start,
            period_end,
            digest.id,
            aggregate.status,
            existing is not None,
        )
        return WeeklyDigestResult(digest=digest, created=existing is None)


async def run_booking_funnel_digest_scheduler() -> None:
    service = BookingFunnelService()
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await service.generate_latest_completed_week(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Booking funnel weekly digest scheduler iteration failed")
        await asyncio.sleep(settings.booking_funnel_digest_scheduler_interval_seconds)
