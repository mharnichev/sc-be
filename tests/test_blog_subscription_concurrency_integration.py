"""Blog transition serialization in isolated PostgreSQL, with Brevo stubbed."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.blog import BlogSubscription, BlogSubscriptionEvent, BlogSubscriptionEventType, BlogSubscriptionStatus
from app.services.blog import BlogSubscriptionService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


pytestmark = pytest.mark.anyio
EMAIL = "synthetic-reader@example.test"
SUBSCRIBED = BlogSubscriptionStatus.subscribed
UNSUBSCRIBED = BlogSubscriptionStatus.unsubscribed
SUBSCRIBE_EVENT = BlogSubscriptionEventType.subscribed
RESUBSCRIBE_EVENT = BlogSubscriptionEventType.resubscribed
UNSUBSCRIBE_EVENT = BlogSubscriptionEventType.unsubscribed


async def bounded(operation):
    return await asyncio.wait_for(operation, timeout=5)


@pytest.fixture
def brevo_stub(database, monkeypatch):
    calls = []

    async def record(kind, subscription):
        # A separate transaction sees the durable row and event before sync.
        async with database() as session:
            persisted = await bounded(session.get(BlogSubscription, subscription.id))
            assert persisted is not None
            events = list(await bounded(session.scalars(select(BlogSubscriptionEvent).where(
                BlogSubscriptionEvent.subscription_id == subscription.id,
            ))))
            assert events
        calls.append((kind, subscription.id, subscription.email, subscription.status))

    async def subscribed(subscription):
        await record("subscribed", subscription)

    async def unsubscribed(subscription):
        await record("unsubscribed", subscription)

    monkeypatch.setattr("app.services.blog.brevo_contact_sync_service", SimpleNamespace(
        sync_subscribed_contact=subscribed, sync_unsubscribed_contact=unsubscribed,
    ))
    return calls


def subscribe_data(**overrides):
    data = dict(
        email="  SyNtHeTiC-ReAdEr@EXAMPLE.TEST  ", name="Synthetic reader", source="synthetic-modal",
        language="uk", referrer="https://example.test/synthetic", utm_source="synthetic-source",
        utm_medium="synthetic-medium", utm_campaign="synthetic-campaign", metadata_json={"synthetic": True},
    )
    data.update(overrides)
    return data


async def seed_subscription(database, status, *, reason=None):
    first = datetime.now(UTC) - timedelta(days=2)
    async with database() as session:
        subscription = BlogSubscription(
            email=EMAIL, name="Original reader", status=status, source="synthetic-original", language="en",
            unsubscribe_token=f"synthetic-token-{uuid4().hex}",
            first_subscribed_at=first, subscribed_at=first,
            unsubscribed_at=first + timedelta(days=1) if status == UNSUBSCRIBED else None,
            unsubscribe_reason=reason, metadata_json={"original": True},
        )
        session.add(subscription)
        await bounded(session.flush())
        session.add(BlogSubscriptionEvent(
            subscription_id=subscription.id, event_type=SUBSCRIBE_EVENT, occurred_at=first,
            source=subscription.source, metadata_json={},
        ))
        if status == UNSUBSCRIBED:
            session.add(BlogSubscriptionEvent(
                subscription_id=subscription.id, event_type=UNSUBSCRIBE_EVENT,
                occurred_at=subscription.unsubscribed_at, source=subscription.source,
                metadata_json={"reason": reason} if reason else {},
            ))
        await bounded(session.commit())
        return subscription.id, subscription.unsubscribe_token, first, subscription.unsubscribed_at


async def persisted_state(database):
    async with database() as session:
        subscriptions = list(await bounded(session.scalars(select(BlogSubscription).order_by(BlogSubscription.id))))
        events = list(await bounded(session.scalars(select(BlogSubscriptionEvent).order_by(BlogSubscriptionEvent.id))))
        return subscriptions, events


def synchronize_initial_reads(monkeypatch, sessions):
    """Force stale plain reads; permit a locking implementation to serialize them."""
    all_started = asyncio.Event()
    leader_read = asyncio.Event()
    all_plain_reads = asyncio.Event()
    leader_committed = asyncio.Event()
    starts, plain_reads, observations = [], [], []
    pids = []

    for index, session in enumerate(sessions):
        execute, commit = session.execute, session.commit

        def install(index, session, execute, commit):
            intercepted = False

            async def execute_with_barrier(statement, *args, **kwargs):
                nonlocal intercepted
                descriptions = getattr(statement, "column_descriptions", ())
                if not intercepted and descriptions and descriptions[0].get("entity") is BlogSubscription:
                    intercepted = True
                    pids.append(await bounded(session.scalar(text("SELECT pg_backend_pid()"))))
                    starts.append(index)
                    if len(starts) == 2:
                        all_started.set()
                    await bounded(all_started.wait())
                    if index == 1:
                        await bounded(leader_read.wait())
                    result = await execute(statement, *args, **kwargs)
                    frozen = result.freeze()
                    row = frozen().scalar_one_or_none()
                    observations.append((index, row.status if row is not None else None))
                    if index == 0:
                        leader_read.set()
                    locking = getattr(statement, "_for_update_arg", None) is not None
                    if row is None or not locking:
                        plain_reads.append(index)
                        if len(plain_reads) == 2:
                            all_plain_reads.set()
                        await bounded(all_plain_reads.wait())
                        if index == 1:
                            await bounded(leader_committed.wait())
                    return frozen()
                return await execute(statement, *args, **kwargs)

            async def commit_with_signal():
                result = await commit()
                if index == 0:
                    leader_committed.set()
                return result

            monkeypatch.setattr(session, "execute", execute_with_barrier)
            monkeypatch.setattr(session, "commit", commit_with_signal)

        install(index, session, execute, commit)
    return observations, pids


async def run_pair(database, monkeypatch, operations):
    async with database() as leader, database() as follower:
        observations, pids = synchronize_initial_reads(monkeypatch, (leader, follower))

        async def attempt(session, operation):
            try:
                result = await bounded(operation(session))
            except Exception as error:
                return error
            assert session.is_active
            assert await bounded(session.scalar(text("SELECT 1"))) == 1
            await bounded(session.commit())
            return result

        results = await asyncio.wait_for(asyncio.gather(
            attempt(leader, operations[0]), attempt(follower, operations[1]),
        ), timeout=10)
        assert len(set(pids)) == 2
        assert sorted(index for index, _status in observations) == [0, 1]
        assert all(not isinstance(result, Exception) for result in results), results
        assert results[0].id == results[1].id
        return results, observations


@pytest.mark.parametrize("scenario", [
    "first_subscribe", "resubscribe", "unsubscribe_email", "unsubscribe_token",
    "repeat_subscribe", "repeat_unsubscribe",
])
async def test_concurrent_duplicate_blog_requests_emit_one_event_per_actual_transition(
    database, monkeypatch, brevo_stub, scenario,
):
    service = BlogSubscriptionService()
    initial = UNSUBSCRIBED if scenario in {"resubscribe", "repeat_unsubscribe"} else SUBSCRIBED
    original = None
    if scenario != "first_subscribe":
        original = await seed_subscription(database, initial)
    subscribing = scenario in {"first_subscribe", "resubscribe", "repeat_subscribe"}

    async def operation(session):
        if subscribing:
            return await service.subscribe(
                session, subscribe_data(confirmation_token=original[1] if original else None), subscriber_ip="192.0.2.1", user_agent="Synthetic test agent",
            )
        return await service.unsubscribe(
            session, token=original[1],
            email=None if scenario == "unsubscribe_token" else " SyNtHeTiC-ReAdEr@EXAMPLE.TEST ",
            reason="Synthetic unsubscribe reason", subscriber_ip="192.0.2.2", user_agent="Synthetic test agent",
        )

    results, observations = await run_pair(database, monkeypatch, (operation, operation))
    subscriptions, events = await persisted_state(database)
    assert len(subscriptions) == 1
    subscription = subscriptions[0]
    assert subscription.email == EMAIL
    assert subscription.status == (SUBSCRIBED if subscribing else UNSUBSCRIBED)
    history = [] if original is None else [SUBSCRIBE_EVENT] + ([UNSUBSCRIBE_EVENT] if initial == UNSUBSCRIBED else [])
    added = {
        "first_subscribe": [SUBSCRIBE_EVENT], "resubscribe": [RESUBSCRIBE_EVENT],
        "unsubscribe_email": [UNSUBSCRIBE_EVENT], "unsubscribe_token": [UNSUBSCRIBE_EVENT],
        "repeat_subscribe": [], "repeat_unsubscribe": [],
    }[scenario]
    assert [event.event_type for event in events] == history + added
    assert all(event.subscription_id == subscription.id for event in events)
    assert all(result.id == subscription.id for result in results)
    assert len(brevo_stub) == 2  # Existing repeat requests also synchronize Brevo.
    assert all(call[0] == ("subscribed" if subscribing else "unsubscribed") for call in brevo_stub)
    if original is None:
        assert all(status is None for _index, status in observations)
    else:
        assert (subscription.id, subscription.unsubscribe_token, subscription.first_subscribed_at) == original[:3]
    if subscribing:
        assert subscription.unsubscribed_at is None
        assert subscription.unsubscribe_reason is None
        assert (subscription.source, subscription.language, subscription.metadata_json) == (
            "synthetic-modal", "uk", {"synthetic": True},
        )
        if added:
            assert events[-1].source == "synthetic-modal"
            assert events[-1].metadata_json["utm_campaign"] == "synthetic-campaign"
    else:
        assert subscription.unsubscribe_reason == "Synthetic unsubscribe reason"
        assert subscription.unsubscribed_at is not None
        if scenario == "repeat_unsubscribe":
            assert subscription.unsubscribed_at == original[3]
        else:
            assert events[-1].occurred_at == subscription.unsubscribed_at


@pytest.mark.parametrize("first", ["subscribe", "unsubscribe"])
async def test_concurrent_opposing_blog_requests_record_both_serialized_transitions(
    database, monkeypatch, brevo_stub, first,
):
    initial = UNSUBSCRIBED if first == "subscribe" else SUBSCRIBED
    original = await seed_subscription(database, initial)
    service = BlogSubscriptionService()

    async def subscribe(session):
        return await service.subscribe(session, subscribe_data(confirmation_token=original[1]))

    async def unsubscribe(session):
        return await service.unsubscribe(session, token=original[1], reason="Synthetic exit")

    operations = (subscribe, unsubscribe) if first == "subscribe" else (unsubscribe, subscribe)
    await run_pair(database, monkeypatch, operations)
    subscriptions, events = await persisted_state(database)
    assert len(subscriptions) == 1
    subscription = subscriptions[0]
    assert (subscription.id, subscription.unsubscribe_token, subscription.first_subscribed_at) == original[:3]
    history = [SUBSCRIBE_EVENT] + ([UNSUBSCRIBE_EVENT] if initial == UNSUBSCRIBED else [])
    expected = [RESUBSCRIBE_EVENT, UNSUBSCRIBE_EVENT] if first == "subscribe" else [UNSUBSCRIBE_EVENT, RESUBSCRIBE_EVENT]
    assert [event.event_type for event in events] == history + expected
    assert subscription.status == (UNSUBSCRIBED if first == "subscribe" else SUBSCRIBED)
    assert len(brevo_stub) == 2
    assert sorted(call[0] for call in brevo_stub) == ["subscribed", "unsubscribed"]
    if first == "subscribe":
        assert subscription.unsubscribe_reason == "Synthetic exit"
        assert events[-1].occurred_at == subscription.unsubscribed_at
    else:
        assert subscription.unsubscribe_reason is None
        assert subscription.unsubscribed_at is None


async def test_repeated_subscribe_keeps_identity_and_non_none_metadata_update_semantics(database, brevo_stub):
    original = await seed_subscription(database, SUBSCRIBED)
    async with database() as session:
        subscription = await bounded(BlogSubscriptionService().subscribe(
            session, subscribe_data(name=None, language=None), subscriber_ip="192.0.2.3", user_agent="Repeat test",
        ))
        assert (subscription.id, subscription.unsubscribe_token, subscription.first_subscribed_at) == original[:3]
        assert subscription.subscribed_at > original[2]
        assert (subscription.name, subscription.language) == ("Original reader", "en")
        assert (subscription.source, subscription.subscriber_ip, subscription.user_agent) == (
            "synthetic-modal", "192.0.2.3", "Repeat test",
        )
    _subscriptions, events = await persisted_state(database)
    assert [event.event_type for event in events] == [SUBSCRIBE_EVENT]
    assert len(brevo_stub) == 1


@pytest.mark.parametrize("existing_reason", [None, "Original reason"])
async def test_repeated_unsubscribe_only_fills_an_empty_reason_without_an_event(
    database, brevo_stub, existing_reason,
):
    original = await seed_subscription(database, UNSUBSCRIBED, reason=existing_reason)
    async with database() as session:
        subscription = await bounded(BlogSubscriptionService().unsubscribe(
            session, token=original[1], reason="Later reason",
        ))
        assert subscription.unsubscribe_reason == (existing_reason or "Later reason")
        assert subscription.unsubscribed_at == original[3]
    _subscriptions, events = await persisted_state(database)
    assert [event.event_type for event in events] == [SUBSCRIBE_EVENT, UNSUBSCRIBE_EVENT]
    assert len(brevo_stub) == 1


async def test_first_subscribe_does_not_recover_a_different_unsubscribe_token_unique_violation(
    database, monkeypatch, brevo_stub,
):
    original = await seed_subscription(database, SUBSCRIBED)
    service = BlogSubscriptionService()
    monkeypatch.setattr(service, "_new_unsubscribe_token", lambda: original[1])
    async with database() as session:
        with pytest.raises(IntegrityError) as error:
            await bounded(service.subscribe(session, subscribe_data(email="other-synthetic@example.test")))
        assert error.value.orig.sqlstate == "23505"
        assert getattr(error.value.orig.__cause__, "constraint_name", None) == "uq_blog_subscriptions_unsubscribe_token"
        await bounded(session.rollback())
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    subscriptions, events = await persisted_state(database)
    assert [subscription.email for subscription in subscriptions] == [EMAIL]
    assert [event.event_type for event in events] == [SUBSCRIBE_EVENT]
    assert brevo_stub == []


@pytest.mark.parametrize("lookup", ["email", "token"])
async def test_missing_proof_or_unknown_unsubscribe_has_no_side_effects(database, brevo_stub, lookup):
    kwargs = {lookup: EMAIL if lookup == "email" else "synthetic-missing-token"}
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await bounded(BlogSubscriptionService().unsubscribe(session, **kwargs))
        expected = ((422, "A personal unsubscribe token is required") if lookup == "email"
                    else (404, "Blog subscription not found"))
        assert (error.value.status_code, error.value.detail) == expected
    assert await persisted_state(database) == ([], [])
    assert brevo_stub == []
