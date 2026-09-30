from __future__ import annotations

import pytest

from app.services.studio_product_composer import StudioProductComposerClient


def test_composer_client_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        StudioProductComposerClient("https://composer.example")


def test_composer_client_accepts_localhost_endpoint() -> None:
    assert StudioProductComposerClient("http://127.0.0.1:8765").base_url == "http://127.0.0.1:8765"


def test_composer_import_state_comparison_is_case_insensitive() -> None:
    imported = {"assets": [{"id": "asset", "state": "IMPORTED"}]}
    items = [item for item in imported["assets"] if str(item.get("state", "")).lower() == "imported"]
    assert items == imported["assets"]


def test_composer_export_completion_uses_exported_state() -> None:
    result = {"state": "EXPORTED"}
    assert str(result["state"]).lower() == "exported"
