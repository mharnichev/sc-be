import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from app import main
from app.core import local_migrations


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_migration_command_uses_project_directory_and_propagates_failure(monkeypatch):
    run = Mock(side_effect=subprocess.CalledProcessError(1, "alembic"))
    monkeypatch.setattr(local_migrations.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        await local_migrations.apply_local_migrations()
    run.assert_called_once_with(
        [sys.executable, "-m", "alembic", "-c", str(local_migrations.PROJECT_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=local_migrations.PROJECT_ROOT,
        check=True,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("environment", ["local", "development", "test", "staging", "production"])
async def test_migrations_finish_before_schedulers_or_requests(monkeypatch, environment):
    from types import SimpleNamespace

    # Stop at the first scheduler gate to prove the startup ordering without
    # starting any background workers or touching a database.
    class StartupSettings(SimpleNamespace):
        @property
        def booking_auto_completion_enabled(self):
            assert migrate.await_count == expected_migrations
            raise RuntimeError("scheduler setup reached")

    migrate = AsyncMock()
    monkeypatch.setattr(main, "settings", StartupSettings(app_env=environment))
    monkeypatch.setattr(main, "apply_local_migrations", migrate)
    # A reload creates another lifespan, so migrations must run again.
    for startup in (1, 2):
        expected_migrations = startup if environment in {"local", "development"} else 0
        with pytest.raises(RuntimeError, match="scheduler setup reached"):
            async with main.lifespan(None):
                pytest.fail("Startup must not accept requests yet")
        assert migrate.await_count == expected_migrations


@pytest.mark.anyio
async def test_failed_migration_prevents_scheduler_setup_and_requests(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(main, "settings", SimpleNamespace(app_env="local"))
    migrate = AsyncMock(side_effect=RuntimeError("migration failed"))
    monkeypatch.setattr(main, "apply_local_migrations", migrate)
    with pytest.raises(RuntimeError, match="migration failed"):
        async with main.lifespan(None):
            pytest.fail("A failed migration must abort startup")
