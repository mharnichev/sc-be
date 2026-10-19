"""Pure contracts: synthetic configuration, no DB/network/provider operations."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.schemas.fiscal import FiscalSubmissionCreate, MockShiftCommand
from app.services import fiscal


@pytest.mark.parametrize('url,env,enabled,expected', [
    ('postgresql+asyncpg://u:p@127.0.0.1/test_fiscal', 'test', True, True),
    ('postgresql+asyncpg://u:p@localhost/test_fiscal', 'production', True, False),
    ('postgresql+asyncpg://u:p@remote/test_fiscal', 'test', True, False),
    ('postgresql+asyncpg://u:p@localhost/soulcuts', 'test', True, False),
    ('sqlite:///:memory:', 'test', True, False),
    ('postgresql+asyncpg://u:p@localhost/test_fiscal', 'test', False, False),
])
def test_actual_session_binding_gate(monkeypatch, url, env, enabled, expected):
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', enabled)
    monkeypatch.setattr(settings, 'app_env', env)
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(url=make_url(url)))
    assert fiscal.available(session) is expected


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_confirmation_must_be_literal_true(value):
    with pytest.raises(ValidationError):
        FiscalSubmissionCreate(preview_id=1, expected_revision=1, snapshot_hash='a' * 64,
            cash_register_id=fiscal.REGISTER, confirm=value)


@pytest.mark.parametrize('value', [True, 1.5, '1'])
def test_revisions_are_strict_integer(value):
    with pytest.raises(ValidationError):
        MockShiftCommand(expected_shift_revision=value)


def test_no_live_transport_or_legal_artifact():
    from pathlib import Path
    source = Path(fiscal.__file__).read_text()
    for forbidden in ('httpx', 'requests.', 'aiohttp', 'api.checkbox', 'nova_poshta', 'messaging'):
        assert forbidden not in source.replace('No messaging service is called.', '')
    doc = SimpleNamespace(snapshot={'items': [{'name': '<script>alert(1)</script>', 'quantity': 1, 'total_minor': 100}], 'amount_minor': 100})
    html = fiscal.print_html(doc)
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert fiscal.NOTICE in html
    assert 'fiscal_number' not in html and 'qr' not in html.lower()
