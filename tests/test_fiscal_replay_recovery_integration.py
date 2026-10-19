"""Accepted consent recovery in disposable PostgreSQL schemas; no transport."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.fiscal import FiscalDocument, FiscalEvent, FiscalJob, MockReceipt
from app.models.fiscal_automation import AutomationCommand, AutomationEvent, FiscalAutomation, MockAutomationRecord
from app.models.order import OrderIntent
from app.schemas.fiscal import FiscalCommand
from app.schemas.fiscal_automation import AutomationCancel
from app.services import fiscal, fiscal_automation as automation
from tests.test_fiscal_integration import approval, preview, sale
from tests.test_fiscal_automation_integration import consent, preview as automation_preview, setup as automation_sale
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def enable_mock(monkeypatch):
    for name in ('checkbox_mock_enabled', 'checkbox_ettn_mock_enabled',
                 'local_sales_simulation_enabled', 'np_mock_shipments_enabled'):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, 'app_env', 'test')


async def approval_state(database, kind, approval_id):
    """Observe every durable consent/dispatch effect, including frozen intents."""
    models = (OrderIntent, FiscalDocument, FiscalJob, MockReceipt, FiscalEvent,
              FiscalAutomation, AutomationCommand, MockAutomationRecord, AutomationEvent)
    async with database() as session:
        counts = tuple([await session.scalar(select(func.count()).select_from(model)) for model in models])
        if kind == 'manual':
            row = await session.get(FiscalDocument, approval_id)
            job = await session.scalar(select(FiscalJob).where(FiscalJob.document_id == approval_id))
            command = (job.status, job.attempts, job.fence)
        else:
            row = await session.get(FiscalAutomation, approval_id)
            job = await session.scalar(select(AutomationCommand).where(
                AutomationCommand.project_id == approval_id, AutomationCommand.operation == 'create'))
            command = (job.id, job.status, job.attempts)
        return counts, (row.id, row.status, row.revision, row.provider_uuid, row.dispatched_at), command


@pytest.mark.parametrize('kind,disabled', [
    ('manual', ('checkbox_mock_enabled',)),
    ('automation', ('checkbox_mock_enabled',)),
    ('automation', ('checkbox_ettn_mock_enabled',)),
    ('automation', ('checkbox_mock_enabled', 'checkbox_ettn_mock_enabled')),
])
async def test_accepted_consent_replay_after_flag_off_recovers_only_frozen_result(
    database, checkout_dependencies, monkeypatch, kind, disabled,
):
    if kind == 'manual':
        order = await sale(database)
        p = await preview(database, order.id)
        body = approval(p, mock_outcome='timeout_after_acceptance')
        create = fiscal.submit
    else:
        order, shipment_id, _ = await automation_sale(database)
        p = await automation_preview(database, order, shipment_id)
        body = consent(p, 'timeout_after_acceptance')
        create = automation.create
    key = str(uuid4())

    async def call(*, raw_key=key, payload=body, actor_id=1, wrong_database=False):
        async with database() as session:
            actor = await session.get(AdminUser, actor_id)
            if wrong_database:
                # The connection remains on the real isolated DB; only its
                # advertised URL changes, so current_database() must reject it.
                url = session.get_bind().url.set(database='different_audit_test')
                with monkeypatch.context() as patch:
                    patch.setattr(session, 'get_bind', lambda: SimpleNamespace(url=url))
                    return await create(session, actor, order.id, payload, raw_key)
            return await create(session, actor, order.id, payload, raw_key)

    accepted = await call()  # Simulate this committed response being lost by tab A.
    frozen = accepted.model_dump(mode='json')
    # Tab B advances the same approval to an unknown accepted provider result.
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        if kind == 'manual':
            unknown = await fiscal.process_job(session, actor, accepted.id)
        else:
            unknown = await automation.command(session, actor, accepted.id,
                FiscalCommand(expected_revision=accepted.revision), str(uuid4()), 'process')
        assert unknown.status == 'unknown'
        assert unknown.provider_uuid == accepted.provider_uuid
    async with database() as session:
        session.add(AdminUser(id=2, email='recovery-other@example.test', hashed_password='unused',
            is_active=True, is_superuser=True))
        await session.commit()
    for name in disabled:
        monkeypatch.setattr(settings, name, False)

    before = await approval_state(database, kind, accepted.id)
    assert (await call()).model_dump(mode='json') == frozen
    assert await approval_state(database, kind, accepted.id) == before

    # A new key or actor has no accepted consent to recover. Changed bodies
    # cannot use the old intent to authorize a different command either.
    for kwargs, conflict in (({'raw_key': str(uuid4())}, False),
                             ({'actor_id': 2}, False),
                             ({'payload': body.model_copy(update={'expected_revision': body.expected_revision + 1})}, True)):
        with pytest.raises(HTTPException) as error:
            await call(**kwargs)
        assert error.value.status_code == 409
        if conflict:
            assert error.value.detail['code'] == 'idempotency_conflict'
    assert await approval_state(database, kind, accepted.id) == before

    # Recovery still requires the current permission, safe environment and
    # actual connected database; it is not a flag-free read authorization.
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        actor.is_superuser, actor.can_fiscalize_sales = False, False
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await call()
    assert error.value.status_code == 403
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        actor.is_superuser = True
        await session.commit()
    with monkeypatch.context() as patch:
        patch.setattr(settings, 'app_env', 'production')
        with pytest.raises(HTTPException) as error:
            await call()
        assert error.value.detail['code'] == 'mock_fiscal_disabled'
    with pytest.raises(HTTPException) as error:
        await call(wrong_database=True)
    assert error.value.detail['code'] == 'mock_fiscal_disabled'
    assert await approval_state(database, kind, accepted.id) == before

    # The recovered approval leads back to lookup of the original UUID, never
    # to another dispatch. Existing automation can also be reconciled/disarmed.
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        if kind == 'manual':
            recovered = await fiscal.process_job(session, actor, accepted.id)
            assert recovered.status == 'issued'
            assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
            assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1
        else:
            recovered = await automation.command(session, actor, accepted.id,
                FiscalCommand(expected_revision=unknown.revision), str(uuid4()), 'process')
            assert recovered.status == 'armed'
            recovered = await automation.command(session, actor, accepted.id,
                FiscalCommand(expected_revision=recovered.revision), str(uuid4()), 'reconcile')
            assert recovered.status == 'armed'
            recovered = await automation.command(session, actor, accepted.id,
                AutomationCancel(expected_revision=recovered.revision), str(uuid4()), 'cancel')
            assert recovered.status == 'cancelled'
            create_command = await session.scalar(select(AutomationCommand).where(
                AutomationCommand.project_id == accepted.id, AutomationCommand.operation == 'create'))
            assert create_command.attempts == 1
            assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 1
            assert await session.scalar(select(func.count()).select_from(FiscalAutomation)) == 1
        assert recovered.provider_uuid == accepted.provider_uuid
    assert (await call()).model_dump(mode='json') == frozen
