"""The new COD/parcel flow also works with actual Alembic constraints/triggers."""
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_provider_sandbox_integration import database
from tests.test_inventory_integration import anyio_backend, checkout_dependencies
from tests.test_mock_cod_fiscal_integration import flags, test_shop_cod_receipt_partial_ttn_return_and_reports as run_flow

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize('strategy', ['manual', 'automation'])
async def test_migrated_cod_and_parcel_provenance(database, checkout_dependencies, strategy):
    async with database() as session:
        head = ScriptDirectory.from_config(Config('alembic.ini')).get_current_head()
        assert await session.scalar(text('SELECT version_num FROM alembic_version')) == head
    await run_flow(database, checkout_dependencies, strategy)
    async with database() as session:
        with pytest.raises(DBAPIError, match='immutable|Immutable|mutation|UPDATE|DELETE'):
            await session.execute(text("UPDATE mock_cod_confirmations SET evidence='changed'"))
        await session.rollback()
        with pytest.raises(DBAPIError, match='immutable|Immutable|mutation|UPDATE|DELETE'):
            await session.execute(text('DELETE FROM mock_shipment_return_receipts'))
        await session.rollback()
