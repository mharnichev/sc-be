from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "APP_ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "a-secure-production-secret-with-32-plus-characters",
        "CORS_ORIGINS": "https://soulcuts.com.ua,https://admin.soulcuts.com.ua",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_config_accepts_explicit_safe_values() -> None:
    settings = production_settings()

    assert settings.app_env == "production"
    assert settings.debug is False
    assert settings.cors_origins == [
        "https://soulcuts.com.ua",
        "https://admin.soulcuts.com.ua",
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"DEBUG": True}, "DEBUG must be false"),
        ({"SECRET_KEY": "change-me"}, "SECRET_KEY must be"),
        (
            {"CORS_ORIGINS": "https://soulcuts.com.ua,http://localhost:3000"},
            "CORS_ORIGINS must not contain localhost",
        ),
    ],
)
def test_production_config_rejects_unsafe_values(
    override: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        production_settings(**override)


def test_waitlist_offer_config_defaults_and_template_validation() -> None:
    settings = Settings(_env_file=None)
    assert settings.waitlist_offer_hold_minutes == 10
    assert settings.waitlist_quiet_hours_from == "20:00"
    assert "{booking_link}" in settings.waitlist_offer_sms_template

    with pytest.raises(ValidationError, match="Unknown waitlist SMS template variables"):
        Settings(_env_file=None, WAITLIST_OFFER_SMS_TEMPLATE="Link: {unknown}")
