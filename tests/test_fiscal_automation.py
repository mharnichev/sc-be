"""ETTN contracts stay strictly inside the local simulator."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.schemas.fiscal_automation import AutomationCreate, AutomationPreviewCreate
from app.services import fiscal_automation as automation


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_literal_future_authorization(value):
    with pytest.raises(ValidationError):
        AutomationCreate(cash_register_id='MOCK-KASA-1', preview_id=1,
            expected_revision=1, snapshot_hash='a' * 64, confirm=value)


@pytest.mark.parametrize('url,env,fiscal_flag,auto_flag,expected', [
    ('postgresql+asyncpg://u:p@127.0.0.1/mock_test', 'test', True, True, True),
    ('postgresql+asyncpg://u:p@localhost/mock_test', 'local', True, True, True),
    ('postgresql+asyncpg://u:p@remote/mock_test', 'test', True, True, False),
    ('postgresql+asyncpg://u:p@localhost/soulcuts', 'test', True, True, False),
    ('postgresql+asyncpg://u:p@localhost/mock_test', 'production', True, True, False),
    ('postgresql+asyncpg://u:p@localhost/mock_test', 'test', False, True, False),
    ('postgresql+asyncpg://u:p@localhost/mock_test', 'test', True, False, False),
    ('sqlite:///:memory:', 'test', True, True, False),
])
def test_explicit_flags_actual_bound_database(monkeypatch, url, env, fiscal_flag, auto_flag, expected):
    monkeypatch.setattr(settings, 'app_env', env)
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', fiscal_flag)
    monkeypatch.setattr(settings, 'checkbox_ettn_mock_enabled', auto_flag)
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(url=make_url(url)))
    assert automation.available(session) is expected


def test_no_transport_or_provider_route():
    source = Path(automation.__file__).read_text()
    for forbidden in ('httpx', 'requests.', 'aiohttp', 'api.checkbox', '/api/v1/ettn', 'messaging', 'np_tracking'):
        assert forbidden not in source
    with pytest.raises(ValidationError):
        AutomationPreviewCreate(cash_register_id='MOCK-KASA-1', shipment_id=1, condition='delivery')
    assert type(settings).model_fields['checkbox_ettn_mock_enabled'].default is False
