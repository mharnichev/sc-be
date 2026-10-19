"""Pure server-gate tests; never connects to a database or provider."""
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.services.local_sales import simulation_available


@pytest.mark.parametrize("env,enabled,url,expected", [
    ("test", True, "postgresql+asyncpg://u:p@127.0.0.1/test_local", True),
    ("local", True, "postgresql+asyncpg://u:p@localhost/test_local", True),
    ("production", True, "postgresql+asyncpg://u:p@localhost/test_local", False),
    ("staging", True, "postgresql+asyncpg://u:p@localhost/test_local", False),
    ("test", False, "postgresql+asyncpg://u:p@localhost/test_local", False),
    ("test", True, "postgresql+asyncpg://u:p@db.example.test/test_local", False),
    ("test", True, "postgresql+asyncpg://u:p@localhost/customer_data", False),
    ("test", True, "sqlite:///test_local.db", False),
])
def test_simulation_gate_uses_actual_bound_database(monkeypatch, env, enabled, url, expected):
    monkeypatch.setattr(settings, "app_env", env)
    monkeypatch.setattr(settings, "local_sales_simulation_enabled", enabled)
    session = SimpleNamespace(bind=SimpleNamespace(url=make_url(url)))
    assert simulation_available(session) is expected
