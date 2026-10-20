"""Release-base compatibility without providers, sockets or application lifespan."""
import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.shop_operations_watch import WATCH_SCHEDULER_NAMES


def test_release_master_flags_remain_closed():
    settings = Settings(_env_file=None)
    assert not settings.master_mono_enabled
    assert not settings.master_mono_invoice_create_enabled
    assert not settings.master_monobank_live_transport_enabled
    assert not settings.master_mono_checkbox_link_confirmed
    assert not settings.mono_checkbox_auto_enabled
    assert not settings.shop_mono_checkbox_enabled


def test_release_preserves_checkout_pilot_validation():
    settings = Settings(_env_file=None, SHOP_CHECKOUT_ENABLED=False,
                        SHOP_CHECKOUT_PILOT_ENABLED=True, SHOP_CHECKOUT_PILOT_CUSTOMER_IDS="17,24")
    assert settings.checkout_pilot_customer_ids == frozenset({17, 24})
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SHOP_CHECKOUT_ENABLED=True, SHOP_CHECKOUT_PILOT_ENABLED=True)


def test_release_scheduler_projection_covers_all_registered_labels_without_lifespan():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app/main.py").read_text())
    names = {node.elts[0].value for node in ast.walk(tree)
             if isinstance(node, ast.Tuple) and len(node.elts) == 2
             and isinstance(node.elts[0], ast.Constant) and isinstance(node.elts[0].value, str)
             and isinstance(node.elts[1], ast.Name) and node.elts[1].id.startswith("run_")}
    assert names == WATCH_SCHEDULER_NAMES
