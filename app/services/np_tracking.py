"""Read-only Nova Poshta tracking; never creates waybills or payment facts."""
import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.shipment import Shipment, TrackingEvent, TrackingPollState

logger = logging.getLogger(__name__)
# Official TrackingDocumentGeneral/getStatusDocuments status table. See API contract docs.
STATUS_MAP = {
    '1': 'preparing', '2': 'cancelled', '3': 'requires_review',
    '4': 'in_transit', '41': 'in_transit', '5': 'in_transit', '6': 'in_transit',
    '7': 'ready_for_pickup', '8': 'ready_for_pickup',
    '9': 'delivered', '10': 'delivered', '11': 'delivered', '12': 'handed_over',
    '15': 'in_transit', '101': 'in_transit', '102': 'returning', '103': 'refused',
    '104': 'requires_review', '105': 'requires_review', '106': 'delivered',
    '107': 'in_transit', '111': 'requires_review', '112': 'requires_review',
    '124': 'requires_review',
}
TERMINAL = {'delivered', 'returned', 'cancelled'}
# Explicit edges: receipt does not prohibit a subsequent refusal/cargo return.
TRANSITIONS = {
    'preparing': {'handed_over', 'in_transit', 'ready_for_pickup', 'delivered', 'refused', 'returning', 'cancelled'},
    'handed_over': {'in_transit', 'ready_for_pickup', 'delivered', 'refused', 'returning', 'cancelled'},
    'in_transit': {'ready_for_pickup', 'delivered', 'refused', 'returning'},
    'ready_for_pickup': {'in_transit', 'delivered', 'refused', 'returning'},
    'delivered': {'refused', 'returning'}, 'refused': {'returning', 'delivered'},
    'returning': {'returned'}, 'returned': set(), 'cancelled': set(),
}


class TrackingError(Exception):
    """Only a fixed safe code; never retain provider body, URL or credentials."""


@dataclass(frozen=True)
class Observation:
    number: str
    raw_status: str
    event_at: datetime | None = None


def aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC) if value else None


def event_time(row):
    candidates = [(row.get('DateScan'), '%Y-%m-%d %H:%M:%S')]
    if str(row.get('StatusCode')) in {'9', '10', '11', '106'}:
        candidates.append((row.get('RecipientDateTime'), '%d.%m.%Y %H:%M:%S'))
    for value, pattern in candidates:
        try:
            parsed = datetime.strptime(value, pattern)
            if parsed.year > 2000:
                return parsed.replace(tzinfo=ZoneInfo('Europe/Kyiv')).astimezone(UTC)
        except (ValueError, TypeError):
            pass
    return None


class NovaPoshtaTracking:
    async def fetch(self, numbers):
        try:
            return await asyncio.wait_for(self._fetch(numbers), timeout=settings.nova_poshta_timeout_seconds)
        except TimeoutError:
            raise TrackingError('timeout') from None

    async def _fetch(self, numbers):
        if not settings.nova_poshta_api_key:
            raise TrackingError('not_configured')
        if not numbers or len(numbers) > 100 or len(set(numbers)) != len(numbers):
            raise TrackingError('invalid_batch')
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(settings.nova_poshta_timeout_seconds)) as client:
                response = await client.post(settings.nova_poshta_api_url, json={
                    'apiKey': settings.nova_poshta_api_key,
                    'modelName': 'TrackingDocumentGeneral', 'calledMethod': 'getStatusDocuments',
                    'methodProperties': {'Documents': [{'DocumentNumber': n} for n in numbers]},
                })
                if response.status_code == 429:
                    raise TrackingError('rate_limited')
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException:
            raise TrackingError('timeout') from None
        except (httpx.HTTPError, ValueError):
            raise TrackingError('unavailable') from None
        if not isinstance(payload, dict) or payload.get('success') is not True or not isinstance(payload.get('data'), list):
            raise TrackingError('invalid_response')
        observations = {}
        for row in payload['data']:
            if not isinstance(row, dict):
                raise TrackingError('invalid_response')
            number, code = row.get('Number'), row.get('StatusCode')
            if number not in numbers or number in observations or not isinstance(code, (str, int)) or isinstance(code, bool):
                raise TrackingError('invalid_response')
            code = str(code)
            if not re.fullmatch(r'[0-9]{1,16}', code):
                raise TrackingError('invalid_response')
            observations[number] = Observation(number, code, event_time(row))
        if set(observations) != set(numbers):
            raise TrackingError('invalid_response')
        return observations


async def apply_observation(session, shipment, observation, now):
    if shipment.simulation or shipment.carrier != 'nova_poshta' or observation.number != shipment.tracking_number:
        raise TrackingError('shipment_mismatch')
    previous_time = aware(shipment.provider_event_at)
    incoming_time = aware(observation.event_at)
    status = STATUS_MAP.get(observation.raw_status, 'requires_review')
    if shipment.return_of_shipment_id is not None:
        if status == 'delivered':
            status = 'returned'
        elif status in {'in_transit', 'ready_for_pickup', 'handed_over'}:
            status = 'returning'
    current = shipment.tracking_status
    anchor = current
    if current == 'requires_review':
        anchor = await session.scalar(select(TrackingEvent.normalized_status).where(
            TrackingEvent.shipment_id == shipment.id,
            TrackingEvent.normalized_status != 'requires_review'
        ).order_by(TrackingEvent.id.desc()).limit(1))
    shipment.last_attempt_at = shipment.last_checked_at = now
    shipment.last_attempt_status = 'success'
    shipment.tracking_failures = 0
    if incoming_time and previous_time and incoming_time < previous_time:
        shipment.last_attempt_status = 'stale_response'
    elif anchor and anchor != status and (
        (not incoming_time or (previous_time and incoming_time == previous_time))
        or (status != 'requires_review' and status not in TRANSITIONS.get(anchor, set()))
    ):
        shipment.last_attempt_status = 'conflicting_response'
    else:
        # Keep the watermark independently of undated observations. Their event
        # journal date remains unknown rather than borrowing an earlier date.
        undated_repeat = incoming_time is None and observation.raw_status == shipment.raw_status and status == current
        shipment.raw_status = observation.raw_status
        shipment.tracking_status = status
        shipment.provider_event_at = incoming_time or previous_time
        key = hashlib.sha256(f'{shipment.id}:{observation.raw_status}:{status}:{incoming_time.isoformat() if incoming_time else ""}'.encode()).hexdigest()
        exists = True if undated_repeat else await session.scalar(select(TrackingEvent.id).where(TrackingEvent.event_key == key))
        if exists is None:
            session.add(TrackingEvent(shipment_id=shipment.id, event_key=key,
                raw_status=observation.raw_status, normalized_status=status,
                event_at=incoming_time, observed_at=now))
    interval = settings.np_tracking_terminal_interval_seconds if shipment.tracking_status in TERMINAL else settings.np_tracking_interval_seconds
    shipment.next_poll_at = now + timedelta(seconds=max(interval, settings.np_tracking_cooldown_seconds))


def mark_failure(shipment, code, now):
    shipment.last_attempt_at = now
    shipment.last_attempt_status = code
    shipment.tracking_failures = (shipment.tracking_failures or 0) + 1
    delay = min(settings.np_tracking_max_backoff_seconds,
                settings.np_tracking_interval_seconds * 2 ** min(shipment.tracking_failures - 1, 16))
    shipment.next_poll_at = now + timedelta(seconds=max(delay, settings.np_tracking_cooldown_seconds))


async def refresh_due(session, order_id=None):
    """Caller owns commit. A global transaction lock covers selection and HTTP.

    PostgreSQL only: durable request spacing survives processes/restarts; row locks
    serialize API/scheduler paths. Customer callers must first authorize order_id.
    """
    if not settings.np_tracking_enabled or not settings.nova_poshta_api_key:
        return 0
    if session.bind.dialect.name != 'postgresql':
        return 0
    if not await session.scalar(text('SELECT pg_try_advisory_xact_lock(721530019)')):
        return 0
    now = datetime.now(UTC)
    await session.execute(insert(TrackingPollState).values(id=1).on_conflict_do_nothing())
    state = await session.get(TrackingPollState, 1, with_for_update=True)
    if state.next_request_at and aware(state.next_request_at) > now:
        return 0
    query = select(Shipment).where(Shipment.simulation.is_(False), Shipment.carrier == 'nova_poshta',
        Shipment.active.is_(True), Shipment.tracking_number.is_not(None),
        or_(Shipment.next_poll_at.is_(None), Shipment.next_poll_at <= now))
    if order_id is not None:
        query = query.where(Shipment.order_id == order_id)
    shipments = list((await session.scalars(query.order_by(Shipment.next_poll_at.asc().nullsfirst(), Shipment.id)
        .limit(min(settings.np_tracking_batch_size, 100)).with_for_update(skip_locked=True))).all())
    valid = []
    for shipment in shipments:
        if not re.fullmatch(r'[0-9]{14}', shipment.tracking_number):
            mark_failure(shipment, 'invalid_tracking_number', now)
        else:
            valid.append(shipment)
    if not valid:
        return 0
    try:
        observations = await NovaPoshtaTracking().fetch([s.tracking_number for s in valid])
        for shipment in valid:
            await apply_observation(session, shipment, observations[shipment.tracking_number], now)
    except TrackingError as error:
        for shipment in valid:
            mark_failure(shipment, str(error), now)
    # Space requests from completion, not start, including failed requests.
    state.next_request_at = datetime.now(UTC) + timedelta(seconds=settings.np_tracking_request_interval_seconds)
    return len(valid)


async def run_np_tracking_scheduler():
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await refresh_due(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log exception text: HTTP exceptions can include credential data.
            logger.warning('Nova Poshta tracking poll failed')
        await asyncio.sleep(settings.np_tracking_poll_seconds)
