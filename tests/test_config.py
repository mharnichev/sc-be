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


@pytest.mark.parametrize("environment", ["local", "development", "test", "staging", "production"])
def test_shop_checkout_is_closed_by_default_and_requires_explicit_enable(monkeypatch, environment):
    monkeypatch.delenv("SHOP_CHECKOUT_ENABLED", raising=False)
    values = {"APP_ENV": environment, "DEBUG": False,
              "SECRET_KEY": "synthetic-production-checkout-secret-at-least-32",
              "CORS_ORIGINS": "https://soulcuts.com.ua", "_env_file": None}
    assert Settings(**values).shop_checkout_enabled is False
    assert Settings(**values, SHOP_CHECKOUT_ENABLED=True).shop_checkout_enabled is True


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


def test_browser_session_ttl_defaults_to_30_days_and_is_bounded() -> None:
    assert Settings(_env_file=None).customer_activity_browser_session_ttl_days == 30
    assert Settings(
        _env_file=None,
        CUSTOMER_ACTIVITY_BROWSER_SESSION_TTL_DAYS=31,
    ).customer_activity_browser_session_ttl_days == 31

    with pytest.raises(ValidationError):
        Settings(_env_file=None, CUSTOMER_ACTIVITY_BROWSER_SESSION_TTL_DAYS=32)


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_production_telegram_bot_requires_webhook_authentication(secret):
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET is required"):
        production_settings(TELEGRAM_BOT_TOKEN="synthetic-test-bot", TELEGRAM_WEBHOOK_SECRET=secret)


def test_production_telegram_bot_accepts_configured_webhook_secret():
    configured = production_settings(TELEGRAM_BOT_TOKEN="synthetic-test-bot", TELEGRAM_WEBHOOK_SECRET="synthetic-webhook-secret")
    assert configured.telegram_webhook_secret == "synthetic-webhook-secret"
