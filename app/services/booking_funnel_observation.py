"""Bounded, client-reported diagnostics. No booking/provider mutations."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from app.models.booking import BarberService
from app.models.booking_funnel_observation import (
    BookingFunnelObservation as Observation, BookingFunnelObservationReceipt as Receipt,
)
from app.services.booking_funnel import _hash_identifier, _hash_session


async def record_observation(session, payload):
    # Do not hash event_id into the content: a retry can use another event ID but
    # must retain its request identity and immutable normalized content.
    content = payload.model_dump(mode="json", exclude={"event_id"})
    payload_hash = _hash_identifier("observation_content", json.dumps(content, sort_keys=True, separators=(",", ":")))
    session_hash = _hash_session(payload.anonymous_session_id)
    event_hash = _hash_identifier("observation_event", payload.event_id)
    request_hash = (_hash_identifier("observation_request", payload.availability_request_id)
                    if payload.availability_request_id else None)
    kind = payload.event_type
    dedup_kind = "terminal" if kind in {"availability_result", "availability_failed", "availability_cancelled"} else kind
    dedup_key = _hash_identifier("observation_dedup", f"{session_hash}:{request_hash or event_hash}:{dedup_kind}")

    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert

    async def receipt(observation_id):
        await session.execute(insert(Receipt).values(
            event_id_hash=event_hash, payload_hash=payload_hash, observation_id=observation_id,
        ).on_conflict_do_nothing())
        saved = (await session.execute(select(Receipt).where(Receipt.event_id_hash == event_hash))).scalar_one()
        if saved.payload_hash != payload_hash:
            raise HTTPException(409, detail="observation_identity_conflict")

    async def existing():
        saved = (await session.execute(select(Receipt).where(Receipt.event_id_hash == event_hash))).scalar_one_or_none()
        if saved is not None:
            if saved.payload_hash != payload_hash:
                raise HTTPException(409, detail="observation_identity_conflict")
            return saved.observation_id
        rows = (await session.execute(select(Observation).where(
            or_(Observation.event_id_hash == event_hash, Observation.dedup_key == dedup_key)
        ))).scalars().all()
        if rows:
            if any(row.payload_hash != payload_hash for row in rows):
                raise HTTPException(409, detail="observation_identity_conflict")
            return rows[0].id
        return None

    if request_hash and session.get_bind().dialect.name == "postgresql":
        # Serialize diagnostics for this request, not booking/resource operations.
        lock_id = int(request_hash[:16], 16)
        if lock_id >= 2 ** 63:
            lock_id -= 2 ** 64
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
    prior_id = await existing()
    if prior_id is not None:
        await receipt(prior_id)
        await session.commit()
        return False
    if request_hash:
        prior = (await session.execute(select(Observation).where(
            Observation.anonymous_session_hash == session_hash,
            Observation.request_id_hash == request_hash,
        ).limit(1))).scalar_one_or_none()
        if prior is not None and (
            prior.master_id, prior.service_ids_key, prior.target_date, prior.duration_minutes, prior.trigger
        ) != (payload.master_id, ",".join(map(str, payload.service_ids)), payload.target_date,
              payload.duration_minutes, payload.trigger):
            raise HTTPException(409, detail="observation_request_context_conflict")
    # Check context against the requested master, not the redirect target.
    services = set((await session.execute(select(BarberService.id).where(
        BarberService.id.in_(payload.service_ids), BarberService.master_id == payload.master_id
    ))).scalars().all())
    if services != set(payload.service_ids):
        raise HTTPException(422, detail="Services must belong to the requested master")
    values = dict(
        event_id_hash=event_hash, dedup_key=dedup_key, payload_hash=payload_hash,
        anonymous_session_hash=session_hash, request_id_hash=request_hash, event_type=kind,
        master_id=payload.master_id, service_ids_key=",".join(map(str, payload.service_ids)),
        target_date=payload.target_date, duration_minutes=payload.duration_minutes,
        trigger=payload.trigger, slot_count=payload.slot_count, error_code=payload.error_code,
        cancel_reason=payload.cancel_reason, occurred_at=datetime.now(ZoneInfo("Europe/Kyiv")),
    )
    try:
        inserted = (await session.execute(insert(Observation).values(**values).on_conflict_do_nothing()
                                           .returning(Observation.id))).scalar_one_or_none()
        if inserted is None:
            # Recheck after the unique conflict waits for concurrent insertion.
            observation_id = await existing()
        else:
            observation_id = inserted
        await receipt(observation_id)
        await session.commit()
        return inserted is not None
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(422, detail="Invalid observation context") from exc
    except Exception:
        await session.rollback()
        raise


async def observation_metrics(session, *, start, end, master_id=None):
    from sqlalchemy import and_, exists, func
    terminal = Observation.__table__.alias("terminal")
    requested = Observation.__table__.alias("requested")
    scope = [Observation.occurred_at >= start, Observation.occurred_at < end]
    if master_id is not None:
        scope.append(Observation.master_id == master_id)
    rows = (await session.execute(select(Observation.event_type, Observation.error_code, func.count()).where(*scope)
                                  .group_by(Observation.event_type, Observation.error_code))).all()
    counts = {kind: 0 for kind in ("time_step_viewed", "availability_requested", "availability_result",
                                  "availability_failed", "availability_cancelled")}
    failures = {code: 0 for code in ("timeout", "network", "http_4xx", "http_5xx", "invalid_response")}
    for kind, code, count in rows:
        counts[kind] += int(count)
        if kind == "availability_failed" and code in failures:
            failures[code] += int(count)
    # All-history request matching: a request just before midnight is not missing
    # merely because its result arrived inside the chosen reporting period.
    missing = (await session.execute(select(func.count()).select_from(terminal).where(
        terminal.c.occurred_at >= start, terminal.c.occurred_at < end,
        terminal.c.event_type.in_(["availability_result", "availability_failed", "availability_cancelled"]),
        *([terminal.c.master_id == master_id] if master_id is not None else []),
        ~exists(select(1).select_from(requested).where(and_(
            requested.c.anonymous_session_hash == terminal.c.anonymous_session_hash,
            requested.c.request_id_hash == terminal.c.request_id_hash,
            requested.c.event_type == "availability_requested",
        ))),
    ))).scalar_one()
    initial_empty = (await session.execute(select(func.count()).select_from(Observation).where(
        *scope, Observation.event_type == "availability_result", Observation.slot_count == 0,
        Observation.trigger == "initial",
    ))).scalar_one()
    return dict(event_counts=counts, failure_counts=failures, initial_empty_results=int(initial_empty),
                terminals_without_request=int(missing))
