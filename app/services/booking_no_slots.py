"""Complete historical check aggregation; identifiers never leave this service."""
from __future__ import annotations

import hashlib
import hmac
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import String, cast, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.booking import BarberService, Master
from app.models.booking_funnel import BookingFunnelEvent as Event, BookingFunnelEventType as Type
from app.schemas.booking_funnel import (
    BookingFunnelNoSlotServiceRef, BookingNoSlotAttempt, BookingNoSlotCheck,
    BookingNoSlotMaster, BookingNoSlotPage,
)


def _services(event):
    key = event.service_ids_key or str(event.service_id or '')
    return sorted({int(x) for x in key.split(',') if x.isdigit() and int(x) > 0})


def _utc(value):
    # SQLite drops timezone information; stored event timestamps are UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _context(event):
    return (event.target_date, tuple(_services(event)), event.duration_minutes)


def _attempt_id(session_hash, master_id, start, end):
    secret = (settings.booking_funnel_hash_secret or settings.secret_key).encode()
    value = f'no-slot-detail:{master_id}:{start.isoformat()}:{end.isoformat()}:{session_hash}'
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()


class BookingNoSlotsService:
    def _filters(self, start, end, master_id=None, unknown_master=False):
        filters = [Event.event_type == Type.no_slot, Event.occurred_at >= start, Event.occurred_at < end]
        if unknown_master:
            filters.append(Event.master_id.is_(None))
        elif master_id is not None:
            filters.append(Event.master_id == master_id)
        return filters

    async def snapshot_id(self, session: AsyncSession) -> int:
        return int((await session.execute(select(func.max(Event.id)))).scalar_one() or 0)

    async def summaries(self, session: AsyncSession, *, start: datetime, end: datetime, master_id=None, snapshot_id=None):
        filters = self._filters(start, end, master_id)
        if snapshot_id is not None:
            filters.append(Event.id <= snapshot_id)
        # Count contexts independently of session deduplication and of legacy response caps.
        contexts = select(Event.master_id, Event.target_date,
            func.coalesce(Event.service_ids_key, cast(Event.service_id, String)).label('services'),
            Event.duration_minutes).where(*filters).distinct().subquery()
        context_counts = dict((await session.execute(select(contexts.c.master_id, func.count()).group_by(contexts.c.master_id))).all())
        rows = (await session.execute(select(
            Event.master_id, Master.full_name, Master.last_name,
            func.count(distinct(Event.anonymous_session_hash)), func.count(),
            func.count(distinct(Event.target_date)), func.min(Event.target_date), func.max(Event.target_date),
            func.max(Event.occurred_at), func.count() - func.count(Event.anonymous_session_hash),
        ).outerjoin(Master, Master.id == Event.master_id).where(*filters)
            .group_by(Event.master_id, Master.full_name, Master.last_name)
            .order_by(func.count(distinct(Event.anonymous_session_hash)).desc(), func.count().desc(), Event.master_id.asc().nulls_last()))).all()
        return [BookingNoSlotMaster(master_id=mid, master_name=' '.join(x for x in (name, last) if x) or None,
            unique_sessions=sessions, observations=count, contexts=context_counts.get(mid, 0), checked_dates=dates,
            date_from=first, date_to=last_date, last_observed_at=observed, unattributed_observations=unattributed)
            for mid, name, last, sessions, count, dates, first, last_date, observed, unattributed in rows]

    async def details(self, session: AsyncSession, *, start: datetime, end: datetime, master_id=None,
                      unknown_master=False, attempt_id=None, offset=0, limit=20, snapshot_id=None, unattributed=False):
        if unattributed and attempt_id is not None:
            raise HTTPException(422, 'Choose attempt_id or unattributed, not both')
        if master_id is None and not unknown_master:
            raise HTTPException(422, 'master_id or unknown_master is required')
        if snapshot_id is None:
            snapshot_id = await self.snapshot_id(session)
        # A frozen upper ID keeps later pages stable as new checks and bookings arrive.
        rows = (await session.execute(select(Event).where(
            *self._filters(start, end, master_id, unknown_master), Event.id <= snapshot_id,
            Event.anonymous_session_hash.is_(None) if unattributed else Event.anonymous_session_hash.is_not(None),
        ).order_by(Event.occurred_at, Event.id))).scalars().all()
        groups = defaultdict(list)
        for row in rows:
            groups[row.anonymous_session_hash].append(row)
        ordered = sorted(groups.items(), key=lambda item: (-_utc(item[1][-1].occurred_at).timestamp(), item[0]))
        selected = rows if unattributed else None
        if attempt_id is not None:
            selected = next((events for key, events in ordered if hmac.compare_digest(_attempt_id(key, master_id, start, end), attempt_id)), None)
            if selected is None:
                raise HTTPException(404, 'Anonymous attempt is not present in this period and master')
        relevant = selected if selected is not None else [event for _, events in ordered[offset:offset + limit] for event in events]
        service_ids = {sid for event in relevant for sid in _services(event)}
        names = dict((await session.execute(select(BarberService.id, func.coalesce(BarberService.title_uk, BarberService.name)).where(BarberService.id.in_(service_ids)))).all()) if service_ids else {}
        def refs(ids):
            return [BookingFunnelNoSlotServiceRef(service_id=sid, service_name=names.get(sid)) for sid in sorted(ids)]
        if selected is not None:
            total = len(selected)
            items = [BookingNoSlotCheck(target_date=e.target_date, services=refs(_services(e)), duration_minutes=e.duration_minutes, observed_at=e.occurred_at) for e in selected[offset:offset + limit]]
        else:
            total = len(ordered)
            page_groups = ordered[offset:offset + limit]
            outcomes = (await session.execute(select(Event.anonymous_session_hash, Event.event_type, Event.master_id, Event.occurred_at).where(
                Event.anonymous_session_hash.in_([key for key, _ in page_groups]),
                Event.event_type.in_([Type.slot_selected, Type.booking_success]), Event.id <= snapshot_id,
            ))).all() if page_groups else []
            items = []
            for key, events in page_groups:
                dates = {e.target_date for e in events if e.target_date is not None}
                first, last = events[0].occurred_at, events[-1].occurred_at
                later = [(kind, mid) for session_key, kind, mid, at in outcomes if session_key == key and _utc(at) > _utc(first)]
                items.append(BookingNoSlotAttempt(
                    attempt_id=_attempt_id(key, master_id, start, end), observations=len(events),
                    contexts=len({_context(e) for e in events}), services=refs({sid for e in events for sid in _services(e)}),
                    durations_minutes=sorted({e.duration_minutes for e in events if e.duration_minutes is not None}),
                    checked_dates=len(dates), date_from=min(dates) if dates else None, date_to=max(dates) if dates else None,
                    first_observed_at=first, last_observed_at=last,
                    later_time_selection=any(kind == Type.slot_selected for kind, _ in later),
                    later_booking_same_master=any(kind == Type.booking_success and mid is not None and mid == master_id for kind, mid in later),
                    later_booking_other_master=any(kind == Type.booking_success and mid is not None and master_id is not None and mid != master_id for kind, mid in later),
                    later_booking_unknown_master=any(kind == Type.booking_success and (mid is None or master_id is None) for kind, mid in later),
                    rapid_checks=sum(0 <= (_utc(b.occurred_at) - _utc(a.occurred_at)).total_seconds() <= 10 for a, b in zip(events, events[1:])),
                ))
        return BookingNoSlotPage(items=items, total=total, offset=offset, limit=limit,
            has_more=offset + len(items) < total, snapshot_id=snapshot_id)
