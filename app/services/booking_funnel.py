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
from sqlalchemy import String, case, cast, distinct, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
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
    counts: dict[BookingFunnelEventType, int],
    conversions: list[BookingFunnelConversionMetric],
    alerts: list[BookingFunnelOperationalAlert],
    thresholds: BookingFunnelThresholdConfig,
) -> BookingFunnelRecommendedAction | None:
    candidates: list[tuple[Decimal, int, BookingFunnelRecommendedAction]] = []
    alert_by_code = {item.code: item for item in alerts}
    starts = counts[BookingFunnelEventType.booking_start]

    booking_error = alert_by_code["booking_error"]
    if booking_error.triggered:
        score = _percent(booking_error.count, starts) or Decimal("100.00")
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
        candidates.append(
            (
                no_slot.rate_percent,
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
        score = _percent(stale_schedule.count, starts) or Decimal("100.00")
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
    latest_digest: BookingFunnelWeeklyDigestResponse | None = None,
) -> BookingFunnelAggregate:
    normalized = {event_type: int(counts.get(event_type, 0)) for event_type in BookingFunnelEventType}
    total = sum(normalized.values())
    threshold_response = BookingFunnelAlertThresholds(
        no_slot_min_count=thresholds.no_slot_min_count,
        no_slot_rate_percent=thresholds.no_slot_rate_percent,
        stale_schedule_count=thresholds.stale_schedule_count,
        booking_error_count=thresholds.booking_error_count,
        meaningful_step_sessions=thresholds.meaningful_step_sessions,
    )
    if total == 0:
        return BookingFunnelAggregate(
            status="empty",
            status_reason="No booking funnel events were recorded in the selected period.",
            steps=[],
            step_to_step_conversion=[],
            overall_conversion=None,
            drop_offs=[],
            operational_alerts=[],
            alert_thresholds=threshold_response,
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
    for from_step, to_step in zip(FUNNEL_STEP_TYPES, FUNNEL_STEP_TYPES[1:]):
        from_count = normalized[from_step]
        to_count = normalized[to_step]
        if from_count <= 0:
            conversion = BookingFunnelConversionMetric(
                from_step=from_step,
                to_step=to_step,
                from_count=from_count,
                to_count=to_count,
                conversion_percent=None,
                status="unavailable",
                unavailable_reason="The preceding step has no recorded sessions.",
            )
        elif to_count > from_count:
            data_anomaly = True
            conversion = BookingFunnelConversionMetric(
                from_step=from_step,
                to_step=to_step,
                from_count=from_count,
                to_count=to_count,
                conversion_percent=None,
                status="unavailable",
                unavailable_reason="The next step exceeds the preceding step; tracking is incomplete.",
            )
        else:
            conversion = BookingFunnelConversionMetric(
                from_step=from_step,
                to_step=to_step,
                from_count=from_count,
                to_count=to_count,
                conversion_percent=_percent(to_count, from_count),
                status="available",
            )
        conversions.append(conversion)
        if conversion.status == "available" and conversion.conversion_percent is not None:
            drop_offs.append(
                BookingFunnelDropOffMetric(
                    from_step=from_step,
                    to_step=to_step,
                    count=from_count - to_count,
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
    successes = normalized[BookingFunnelEventType.booking_success]
    if starts <= 0:
        overall = BookingFunnelOverallConversion(
            started=starts,
            succeeded=successes,
            conversion_percent=None,
            status="unavailable",
            unavailable_reason="No booking_start baseline was recorded.",
        )
    elif successes > starts:
        data_anomaly = True
        overall = BookingFunnelOverallConversion(
            started=starts,
            succeeded=successes,
            conversion_percent=None,
            status="unavailable",
            unavailable_reason="Server-side successes exceed recorded starts; session attribution is incomplete.",
        )
    else:
        overall = BookingFunnelOverallConversion(
            started=starts,
            succeeded=successes,
            conversion_percent=_percent(successes, starts),
            status="available",
        )

    master_selected = normalized[BookingFunnelEventType.master_selected]
    no_slot_rate = _percent(normalized[BookingFunnelEventType.no_slot], master_selected)
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
    recommendation = _recommend_action(normalized, conversions, alerts, thresholds)
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

    if starts == 0:
        aggregate_status = "unavailable"
        status_reason = "Events exist, but booking_start was not recorded."
    elif data_anomaly or unattributed_booking_successes:
        aggregate_status = "partial"
        status_reason = (
            "Some server-side successes lack a matching anonymous session or step counts are non-monotonic."
        )
    else:
        aggregate_status = "available"
        status_reason = None

    return BookingFunnelAggregate(
        status=aggregate_status,
        status_reason=status_reason,
        steps=steps,
        step_to_step_conversion=conversions,
        overall_conversion=overall,
        drop_offs=drop_offs,
        operational_alerts=alerts,
        alert_thresholds=threshold_response,
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
        values = {
            "event_id_hash": _hash_identifier("event", payload.event_id),
            "event_type": payload.event_type,
            "source": BookingFunnelEventSource.client,
            "anonymous_session_hash": _hash_session(payload.anonymous_session_id),
            "master_id": payload.master_id,
            "service_id": payload.service_id,
            "booking_id": None,
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
        event = BookingFunnelEvent(
            event_id_hash=_hash_identifier("event", f"server:booking:{booking_id}"),
            event_type=BookingFunnelEventType.booking_success,
            source=BookingFunnelEventSource.server,
            anonymous_session_hash=(
                _hash_session(anonymous_session_id)
                if anonymous_session_id is not None
                else None
            ),
            master_id=master_id,
            service_id=service_id,
            booking_id=booking_id,
            occurred_at=occurred_at or datetime.now(KYIV_TZ),
        )
        session.add(event)
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
        identity = case(
            (
                BookingFunnelEvent.event_type == BookingFunnelEventType.booking_success,
                cast(BookingFunnelEvent.booking_id, String),
            ),
            else_=BookingFunnelEvent.anonymous_session_hash,
        )
        unattributed = func.sum(
            case(
                (
                    (
                        BookingFunnelEvent.event_type == BookingFunnelEventType.booking_success
                    )
                    & BookingFunnelEvent.anonymous_session_hash.is_(None),
                    1,
                ),
                else_=0,
            )
        )
        statement = (
            select(
                BookingFunnelEvent.event_type,
                func.count(distinct(identity)),
                unattributed,
            )
            .where(
                BookingFunnelEvent.occurred_at >= start,
                BookingFunnelEvent.occurred_at < end,
            )
            .group_by(BookingFunnelEvent.event_type)
        )
        if master_id is not None:
            statement = statement.where(BookingFunnelEvent.master_id == master_id)
        rows = (await session.execute(statement)).all()
        counts = {event_type: 0 for event_type in BookingFunnelEventType}
        unattributed_successes = 0
        for event_type, count, unattributed_count in rows:
            normalized_type = (
                event_type
                if isinstance(event_type, BookingFunnelEventType)
                else BookingFunnelEventType(str(event_type))
            )
            counts[normalized_type] = int(count or 0)
            if normalized_type == BookingFunnelEventType.booking_success:
                unattributed_successes = int(unattributed_count or 0)
        latest = await self.latest_digest(session) if include_latest_digest else None
        return build_funnel_aggregate(
            counts,
            unattributed_booking_successes=unattributed_successes,
            thresholds=self.thresholds,
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
        if existing is not None:
            await session.commit()
            logger.info(
                "Booking funnel weekly digest already exists period_start=%s period_end=%s digest_id=%s",
                period_start,
                period_end,
                existing.id,
            )
            return WeeklyDigestResult(digest=existing, created=False)

        start = datetime.combine(period_start, time.min, tzinfo=KYIV_TZ)
        end = datetime.combine(period_end + timedelta(days=1), time.min, tzinfo=KYIV_TZ)
        aggregate = await self.aggregate(
            session,
            start=start,
            end=end,
            include_latest_digest=False,
        )
        generated_at = now_kyiv
        digest = BookingFunnelWeeklyDigest(
            period_start=period_start,
            period_end=period_end,
            generated_at=generated_at,
            data_status=aggregate.status,
            insight_uk=aggregate.weekly_insight_uk,
            recommended_action_code=(
                aggregate.recommended_action.code if aggregate.recommended_action else None
            ),
            recommended_action_uk=(
                aggregate.recommended_action.explanation_uk
                if aggregate.recommended_action
                else None
            ),
            payload_json={
                "recommended_action": (
                    aggregate.recommended_action.model_dump(mode="json")
                    if aggregate.recommended_action
                    else None
                ),
                "step_counts": [item.model_dump(mode="json") for item in aggregate.steps],
                "operational_alerts": [
                    item.model_dump(mode="json") for item in aggregate.operational_alerts
                ],
            },
        )
        session.add(digest)
        try:
            await session.commit()
            await session.refresh(digest)
        except Exception:
            await session.rollback()
            raise
        logger.info(
            "Booking funnel weekly digest created period_start=%s period_end=%s digest_id=%s status=%s",
            period_start,
            period_end,
            digest.id,
            aggregate.status,
        )
        return WeeklyDigestResult(digest=digest, created=True)


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
