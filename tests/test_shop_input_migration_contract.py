"""Offline migration contracts supplement the real PostgreSQL rehearsal."""
import importlib.util
from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.core.database import Base


def test_input_revision_is_single_additive_head_and_only_changes_admission_constraint():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'alembic'))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ['0139_shop_input_guards']
    revision = script.get_revision('0139_shop_input_guards')
    assert revision.down_revision == '0138_shop_content_en'
    spec = importlib.util.spec_from_file_location('input_migration', revision.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    module.op = Operations(MigrationContext.configure(dialect_name='postgresql', opts={
        'as_sql': True, 'output_buffer': output, 'target_metadata': Base.metadata,
    }))
    module.upgrade()
    upgrade = output.getvalue()
    assert upgrade.count('ALTER TABLE shop_ingress_admissions') == 2
    assert 'CREATE TABLE' not in upgrade and 'ADD COLUMN' not in upgrade
    for action in ('otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart'):
        assert repr(action) in upgrade
    output.seek(0)
    output.truncate()
    module.downgrade()
    downgrade = output.getvalue()
    assert downgrade.startswith('DELETE FROM shop_ingress_admissions WHERE action NOT IN')
    assert "action IN ('otp_send', 'otp_verify', 'checkout')" in downgrade
    assert 'ck_shop_ingress_admissions_action' in upgrade
    assert 'ck_shop_ingress_admissions_action' in downgrade
