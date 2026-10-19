"""Run the real startup shell with fake executables: no app, DB or network."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
from alembic.config import Config
from alembic.script import ScriptDirectory
_release_config=Config(str(ROOT/'alembic.ini'))
_release_config.set_main_option('script_location',str(ROOT/'alembic'))
RELEASE_HEAD=ScriptDirectory.from_config(_release_config).get_current_head()


def run_start(tmp_path, *, environment='production', fail='', **overrides):
    log = tmp_path / 'commands'
    for name in ('python', 'alembic', 'uvicorn', 'sleep'):
        executable = tmp_path / name
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >> "$COMMAND_LOG"\n'
                              'if [ "' + name + ' $*" = "$FAIL_COMMAND" ]; then exit 1; fi\n')
        executable.chmod(0o700)
    env = {'PATH': str(tmp_path), 'APP_ENV': environment, 'COMMAND_LOG': str(log),
           'FAIL_COMMAND': fail, 'DATABASE_WAIT_ATTEMPTS': '2', **overrides}
    result = subprocess.run(['/bin/sh', str(ROOT / 'scripts/start.sh')], env=env,
                            capture_output=True, text=True, timeout=5)
    return result, log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize('environment', ['production', 'staging'])
def test_replicas_only_check_schema_and_never_apply(tmp_path, environment):
    result, calls = run_start(tmp_path, environment=environment)
    assert result.returncode == 0
    assert calls == ['python -m app.core.proxy_trust','python -m app.core.migration_readiness',
                     'uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 --workers 1']


def test_explicit_migration_owner_preserves_failure(tmp_path):
    result, calls = run_start(tmp_path, MIGRATIONS_MODE='apply', fail='alembic upgrade head')
    assert result.returncode != 0
    assert calls == ['python -m app.core.proxy_trust','alembic current', 'alembic upgrade head']


def test_schema_check_timeout_never_starts_api(tmp_path):
    result, calls = run_start(tmp_path, fail='python -m app.core.migration_readiness')
    assert result.returncode != 0
    assert calls == ['python -m app.core.proxy_trust','python -m app.core.migration_readiness', 'sleep 1',
                     'python -m app.core.migration_readiness']
    assert 'API will not start' in result.stderr


def test_local_keeps_automatic_migrations_and_reload(tmp_path):
    result, calls = run_start(tmp_path, environment='local')
    assert result.returncode == 0
    assert calls == ['python -m app.core.proxy_trust','alembic current', 'alembic upgrade head',
                     'uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 --reload']


@pytest.mark.parametrize('overrides', [{'MIGRATIONS_MODE': 'skip'},
    {'DATABASE_WAIT_ATTEMPTS': '0'}, {'DATABASE_WAIT_ATTEMPTS': '-1'},
    {'DATABASE_WAIT_ATTEMPTS': 'oops'}])
def test_invalid_startup_controls_fail_before_any_command(tmp_path, overrides):
    result, calls = run_start(tmp_path, **overrides)
    assert result.returncode != 0 and calls == []

@pytest.mark.parametrize('revisions,accepted', [([],False),
    (['0127_shop_np_returns'],False),(['future_unknown_revision'],False),
    ([RELEASE_HEAD],True),
    ([RELEASE_HEAD,'0127_shop_np_returns'],False)])
def test_schema_gate_reads_exact_head_without_writing(revisions, accepted):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, event, text
    from app.core.migration_readiness import require_current_schema
    config=Config(str(ROOT/'alembic.ini'));config.set_main_option('script_location',str(ROOT/'alembic'))
    script=ScriptDirectory.from_config(config)
    engine=create_engine('sqlite:///:memory:');statements=[]
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)'))
        for revision in revisions:
            connection.execute(text('INSERT INTO alembic_version VALUES (:revision)'),{'revision':revision})
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,statement,parameters,context,executemany:statements.append(statement))
    with engine.connect() as connection:
        if accepted:require_current_schema(connection,script)
        else:
            with pytest.raises(RuntimeError,match='release head'):require_current_schema(connection,script)
    assert statements and all(s.lstrip().upper().startswith(('SELECT','PRAGMA')) for s in statements)
    engine.dispose()


def test_schema_probe_driver_errors_do_not_log_connection_secrets(monkeypatch, capsys):
    from app.core import migration_readiness
    def fail(*args, **kwargs):
        assert kwargs['connect_args']['options'] == '-c statement_timeout=5000 -c lock_timeout=5000'
        raise ValueError('sensitive-connection-value')
    monkeypatch.setattr(migration_readiness,'create_engine',fail)
    assert migration_readiness.main() == 1
    output=capsys.readouterr()
    assert 'sensitive-connection-value' not in output.err
    assert 'Schema check failed' in output.err


@pytest.mark.parametrize('value',['*','0.0.0.0/0','::/0','','127.0.0.1,','proxy.example.com','10.20.30.1/24'])
def test_proxy_trust_rejects_unbounded_or_ambiguous_sources(value):
    from app.core.proxy_trust import validate_proxy_allowlist
    with pytest.raises(ValueError):validate_proxy_allowlist(value)

@pytest.mark.parametrize('value',['127.0.0.1','127.0.0.1,::1','10.20.30.0/24'])
def test_proxy_trust_accepts_explicit_networks(value):
    from app.core.proxy_trust import validate_proxy_allowlist
    validate_proxy_allowlist(value)

def test_proxy_validation_failure_prevents_database_and_api(tmp_path):
    result,calls=run_start(tmp_path,fail='python -m app.core.proxy_trust')
    assert result.returncode!=0 and calls==['python -m app.core.proxy_trust']
