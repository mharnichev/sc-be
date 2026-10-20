"""Explicit release migration round-trip on the private disposable PG template."""
import asyncio

import pytest
from alembic import command
from sqlalchemy import text

from tests.test_shop_fiscal_integration import template_database


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_0129_through_0134_clean_upgrade_downgrade_upgrade(template_database):
    _, name, admin, config = template_database
    assert name.startswith("shop_fiscal_test_template_")
    # The shared fixture has already upgraded from empty, downgraded to 0122,
    # and upgraded to head. Repeat the exact release boundary explicitly.
    print("RELEASE MIGRATIONS: empty -> 0134 -> 0122 -> 0134 fixture completed")
    await asyncio.to_thread(command.downgrade, config, "0129_shop_ingress_limits")
    await asyncio.to_thread(command.upgrade, config, "0134_shop_canonical_collation")
    print("RELEASE MIGRATIONS: initial 0129 -> 0134 PASS")
    await asyncio.to_thread(command.downgrade, config, "0129_shop_ingress_limits")
    print("RELEASE MIGRATIONS: 0134 -> 0129 PASS")
    await asyncio.to_thread(command.upgrade, config, "0134_shop_canonical_collation")
    print("RELEASE MIGRATIONS: 0129 -> 0134 PASS")
    from app.core.config import settings
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0134_shop_canonical_collation"
            assert await connection.scalar(text("SELECT count(*) FROM master_mono_create_requests")) == 0
    finally:
        await engine.dispose()
    print("RELEASE MIGRATIONS: final head 0134, clean dispatch ledger PASS")
