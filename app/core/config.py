from __future__ import annotations

from functools import lru_cache
from string import Formatter
from typing import Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="barbershop-platform", alias="APP_NAME")
    app_env: Literal["local", "development", "staging", "production", "test"] = Field(
        default="local",
        alias="APP_ENV",
    )
    debug: bool = Field(default=True, alias="DEBUG")
    secret_key: str = Field(default="change-me", alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(default=60, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    backoffice_refresh_token_expire_days: int = Field(default=7, alias="BACKOFFICE_REFRESH_TOKEN_EXPIRE_DAYS")
    customer_access_token_expire_days: int = Field(default=30, alias="CUSTOMER_ACCESS_TOKEN_EXPIRE_DAYS")
    otp_code_ttl_minutes: int = Field(default=10, alias="OTP_CODE_TTL_MINUTES")
    otp_resend_interval_seconds: int = Field(default=120, alias="OTP_RESEND_INTERVAL_SECONDS")
    otp_max_sends_per_day: int = Field(default=3, alias="OTP_MAX_SENDS_PER_DAY")
    otp_max_verify_attempts_per_day: int = Field(default=5, alias="OTP_MAX_VERIFY_ATTEMPTS_PER_DAY")

    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="barbershop_platform", alias="POSTGRES_DB")
    postgres_user: str = Field(default="postgres", alias="POSTGRES_USER")
    postgres_password: str = Field(default="postgres", alias="POSTGRES_PASSWORD")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    cors_origins: list[str] | str = Field(
        default=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3001",
            "http://localhost:3002",
            "http://127.0.0.1:3002",
            "http://localhost:3003",
            "http://127.0.0.1:3003",
            "http://localhost:3004",
            "http://127.0.0.1:3004",
            "http://localhost:4040",
            "http://127.0.0.1:4040",
            "http://localhost:3010",
            "http://127.0.0.1:3010",
            "http://localhost:6060",
            "http://127.0.0.1:6060",
        ],
        alias="CORS_ORIGINS",
    )

    aws_region: str | None = Field(default=None, alias="AWS_REGION")
    aws_s3_bucket: str | None = Field(default=None, alias="AWS_S3_BUCKET")
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    sms_provider: str = Field(default="stub", alias="SMS_PROVIDER")
    sms_sender_name: str | None = Field(default="Soul Cuts", alias="SMS_SENDER_NAME")
    sms_club_token: str | None = Field(default=None, alias="SMS_CLUB_TOKEN")
    sms_club_base_url: str = Field(default="https://im.smsclub.mobi", alias="SMS_CLUB_BASE_URL")
    sms_delivery_status_scheduler_enabled: bool = Field(
        default=True,
        alias="SMS_DELIVERY_STATUS_SCHEDULER_ENABLED",
    )
    sms_delivery_status_poll_interval_seconds: int = Field(
        default=60,
        ge=15,
        alias="SMS_DELIVERY_STATUS_POLL_INTERVAL_SECONDS",
    )
    sms_delivery_status_max_age_hours: int = Field(
        default=48,
        ge=1,
        le=168,
        alias="SMS_DELIVERY_STATUS_MAX_AGE_HOURS",
    )
    sms_otp_template: str = Field(
        default="Ваш код для входу в Soul Cuts: {code}. Нікому його не повідомляйте.",
        alias="SMS_OTP_TEMPLATE",
    )
    booking_sms_notifications_enabled: bool = Field(default=False, alias="BOOKING_SMS_NOTIFICATIONS_ENABLED")
    booking_sms_reminders_enabled: bool = Field(default=False, alias="BOOKING_SMS_REMINDERS_ENABLED")
    booking_sms_two_hour_reminders_enabled: bool = Field(default=True, alias="BOOKING_SMS_TWO_HOUR_REMINDERS_ENABLED")
    booking_sms_two_hour_reminder_lead_hours: int = Field(default=2, ge=1, alias="BOOKING_SMS_TWO_HOUR_REMINDER_LEAD_HOURS")
    booking_sms_two_hour_reminder_window_minutes: int = Field(
        default=30,
        ge=1,
        alias="BOOKING_SMS_TWO_HOUR_REMINDER_WINDOW_MINUTES",
    )
    booking_sms_confirmation_template: str = Field(
        default=(
            "Ви записані до майстра {master_name} на {appointment_date} о {appointment_time}. "
            "Чекаємо у {barbershop_name}."
        ),
        alias="BOOKING_SMS_CONFIRMATION_TEMPLATE",
    )
    booking_sms_two_hour_reminder_template: str = Field(
        default=(
            "Нагадуємо, сьогодні о {appointment_time} у вас візит до майстра {master_name}. "
            "Будемо раді бачити вас у {barbershop_name}."
        ),
        alias="BOOKING_SMS_TWO_HOUR_REMINDER_TEMPLATE",
    )
    waitlist_request_expiry_days: int = Field(default=90, ge=1, le=120, alias="WAITLIST_REQUEST_EXPIRY_DAYS")
    waitlist_offer_hold_minutes: int = Field(default=10, ge=2, le=30, alias="WAITLIST_OFFER_HOLD_MINUTES")
    waitlist_offer_frequency_minutes: int = Field(
        default=60,
        ge=0,
        le=1440,
        alias="WAITLIST_OFFER_FREQUENCY_MINUTES",
    )
    waitlist_offer_scheduler_enabled: bool = Field(default=True, alias="WAITLIST_OFFER_SCHEDULER_ENABLED")
    waitlist_offer_scheduler_interval_seconds: int = Field(
        default=60,
        ge=15,
        alias="WAITLIST_OFFER_SCHEDULER_INTERVAL_SECONDS",
    )
    waitlist_quiet_hours_from: str = Field(default="20:00", alias="WAITLIST_QUIET_HOURS_FROM")
    waitlist_quiet_hours_to: str = Field(default="10:00", alias="WAITLIST_QUIET_HOURS_TO")
    waitlist_offer_public_path: str = Field(
        default="/booking/waitlist-offer",
        alias="WAITLIST_OFFER_PUBLIC_PATH",
    )
    waitlist_offer_sms_template: str = Field(
        default=(
            "Звільнився час у {master_name}: {appointment_date} о {appointment_time}. "
            "Підтвердіть запис протягом {hold_minutes} хв: {booking_link}"
        ),
        alias="WAITLIST_OFFER_SMS_TEMPLATE",
    )
    email_notifications_enabled: bool = Field(default=False, alias="EMAIL_NOTIFICATIONS_ENABLED")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    smtp_from_email: str | None = Field(default=None, alias="SMTP_FROM_EMAIL")
    smtp_from_name: str = Field(default="Soulcuts", alias="SMTP_FROM_NAME")
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")
    smtp_timeout_seconds: int = Field(default=10, alias="SMTP_TIMEOUT_SECONDS")
    brevo_sync_enabled: bool = Field(default=False, alias="BREVO_SYNC_ENABLED")
    brevo_api_key: str | None = Field(default=None, alias="BREVO_API_KEY")
    brevo_contact_list_id: int | None = Field(default=None, alias="BREVO_CONTACT_LIST_ID")
    brevo_api_base_url: str = Field(default="https://api.brevo.com/v3", alias="BREVO_API_BASE_URL")
    brevo_timeout_seconds: int = Field(default=10, alias="BREVO_TIMEOUT_SECONDS")
    google_business_client_id: str | None = Field(default=None, alias="GOOGLE_BUSINESS_CLIENT_ID")
    google_business_client_secret: str | None = Field(default=None, alias="GOOGLE_BUSINESS_CLIENT_SECRET")
    google_business_refresh_token: str | None = Field(default=None, alias="GOOGLE_BUSINESS_REFRESH_TOKEN")
    google_business_account_id: str | None = Field(default=None, alias="GOOGLE_BUSINESS_ACCOUNT_ID")
    google_business_location_id: str | None = Field(default=None, alias="GOOGLE_BUSINESS_LOCATION_ID")
    google_business_reviews_cache_ttl_days: int = Field(default=30, alias="GOOGLE_BUSINESS_REVIEWS_CACHE_TTL_DAYS")
    google_business_reviews_page_size: int = Field(default=50, alias="GOOGLE_BUSINESS_REVIEWS_PAGE_SIZE")
    google_business_reviews_max_pages: int = Field(default=5, alias="GOOGLE_BUSINESS_REVIEWS_MAX_PAGES")
    google_business_reviews_order_by: str = Field(default="updateTime desc", alias="GOOGLE_BUSINESS_REVIEWS_ORDER_BY")
    nova_poshta_api_key: str | None = Field(default=None, alias="NOVA_POSHTA_API_KEY")
    nova_poshta_api_url: str = Field(default="https://api.novaposhta.ua/v2.0/json/", alias="NOVA_POSHTA_API_URL")
    nova_poshta_timeout_seconds: int = Field(default=10, alias="NOVA_POSHTA_TIMEOUT_SECONDS")
    product_top_scheduler_enabled: bool = Field(default=True, alias="PRODUCT_TOP_SCHEDULER_ENABLED")
    product_top_check_interval_days: int = Field(default=7, ge=1, alias="PRODUCT_TOP_CHECK_INTERVAL_DAYS")
    product_top_refresh_interval_days: int = Field(default=30, ge=1, alias="PRODUCT_TOP_REFRESH_INTERVAL_DAYS")
    product_top_window_days: int = Field(default=30, ge=1, alias="PRODUCT_TOP_WINDOW_DAYS")
    product_top_fraction: float = Field(default=0.1, gt=0, le=1, alias="PRODUCT_TOP_FRACTION")
    product_top_max_products: int = Field(default=8, ge=1, alias="PRODUCT_TOP_MAX_PRODUCTS")
    product_top_min_unique_views: int = Field(default=30, ge=0, alias="PRODUCT_TOP_MIN_UNIQUE_VIEWS")
    product_top_min_paid_orders: int = Field(default=3, ge=0, alias="PRODUCT_TOP_MIN_PAID_ORDERS")
    product_view_retention_days: int = Field(default=90, ge=30, alias="PRODUCT_VIEW_RETENTION_DAYS")
    service_popularity_scheduler_enabled: bool = Field(
        default=True,
        alias="SERVICE_POPULARITY_SCHEDULER_ENABLED",
    )
    service_popularity_check_interval_days: int = Field(
        default=7,
        ge=1,
        alias="SERVICE_POPULARITY_CHECK_INTERVAL_DAYS",
    )
    service_popularity_refresh_interval_days: int = Field(
        default=30,
        ge=1,
        alias="SERVICE_POPULARITY_REFRESH_INTERVAL_DAYS",
    )
    service_popularity_window_days: int = Field(
        default=30,
        ge=1,
        alias="SERVICE_POPULARITY_WINDOW_DAYS",
    )
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_bot_username: str | None = Field(default=None, alias="TELEGRAM_BOT_USERNAME")
    telegram_api_base_url: str = Field(default="https://api.telegram.org", alias="TELEGRAM_API_BASE_URL")
    telegram_send_timeout_seconds: int = Field(default=10, alias="TELEGRAM_SEND_TIMEOUT_SECONDS")
    telegram_webhook_secret: str | None = Field(default=None, alias="TELEGRAM_WEBHOOK_SECRET")
    public_api_base_url: str | None = Field(default=None, alias="PUBLIC_API_BASE_URL")
    public_site_url: str = Field(default="https://soulcuts.com.ua", alias="PUBLIC_SITE_URL")
    messaging_max_retry_attempts: int = Field(default=3, alias="MESSAGING_MAX_RETRY_ATTEMPTS")
    messaging_retry_delay_minutes: int = Field(default=15, alias="MESSAGING_RETRY_DELAY_MINUTES")
    messaging_batch_size: int = Field(default=50, alias="MESSAGING_BATCH_SIZE")
    messaging_default_review_url: str | None = Field(default=None, alias="MESSAGING_DEFAULT_REVIEW_URL")
    review_request_delay_minutes: int = Field(default=0, ge=0, alias="REVIEW_REQUEST_DELAY_MINUTES")
    review_daily_send_time: str = Field(default="10:00", alias="REVIEW_DAILY_SEND_TIME")
    review_request_lookback_hours: int = Field(default=24, ge=1, le=168, alias="REVIEW_REQUEST_LOOKBACK_HOURS")
    review_token_ttl_days: int = Field(default=14, ge=1, le=30, alias="REVIEW_TOKEN_TTL_DAYS")
    review_comment_max_length: int = Field(default=2000, ge=100, le=10000, alias="REVIEW_COMMENT_MAX_LENGTH")
    review_frequency_cap_days: int = Field(default=90, ge=0, alias="REVIEW_FREQUENCY_CAP_DAYS")
    review_submitted_frequency_cap_days: int = Field(
        default=270,
        ge=0,
        alias="REVIEW_SUBMITTED_FREQUENCY_CAP_DAYS",
    )
    review_quiet_hours_from: str = Field(default="20:00", alias="REVIEW_QUIET_HOURS_FROM")
    review_quiet_hours_to: str = Field(default="10:00", alias="REVIEW_QUIET_HOURS_TO")
    review_request_scheduler_enabled: bool = Field(default=True, alias="REVIEW_REQUEST_SCHEDULER_ENABLED")
    review_request_scheduler_interval_seconds: int = Field(
        default=60,
        ge=15,
        alias="REVIEW_REQUEST_SCHEDULER_INTERVAL_SECONDS",
    )
    review_validation_rate_limit: int = Field(default=30, ge=1, alias="REVIEW_VALIDATION_RATE_LIMIT")
    review_submission_rate_limit: int = Field(default=5, ge=1, alias="REVIEW_SUBMISSION_RATE_LIMIT")
    review_public_author_names_enabled: bool = Field(default=False, alias="REVIEW_PUBLIC_AUTHOR_NAMES_ENABLED")
    review_public_path: str = Field(default="/masters", alias="REVIEW_PUBLIC_PATH")
    booking_funnel_hash_secret: str | None = Field(default=None, alias="BOOKING_FUNNEL_HASH_SECRET")
    booking_funnel_event_rate_limit: int = Field(
        default=120,
        ge=1,
        alias="BOOKING_FUNNEL_EVENT_RATE_LIMIT",
    )
    booking_funnel_digest_scheduler_enabled: bool = Field(
        default=True,
        alias="BOOKING_FUNNEL_DIGEST_SCHEDULER_ENABLED",
    )
    booking_funnel_digest_scheduler_interval_seconds: int = Field(
        default=3600,
        ge=60,
        alias="BOOKING_FUNNEL_DIGEST_SCHEDULER_INTERVAL_SECONDS",
    )
    booking_funnel_no_slot_alert_min_count: int = Field(
        default=3,
        ge=1,
        alias="BOOKING_FUNNEL_NO_SLOT_ALERT_MIN_COUNT",
    )
    booking_funnel_no_slot_alert_rate_percent: float = Field(
        default=20.0,
        ge=0,
        le=100,
        alias="BOOKING_FUNNEL_NO_SLOT_ALERT_RATE_PERCENT",
    )
    booking_funnel_stale_schedule_alert_count: int = Field(
        default=1,
        ge=1,
        alias="BOOKING_FUNNEL_STALE_SCHEDULE_ALERT_COUNT",
    )
    booking_funnel_error_alert_count: int = Field(
        default=1,
        ge=1,
        alias="BOOKING_FUNNEL_ERROR_ALERT_COUNT",
    )
    booking_funnel_meaningful_step_sessions: int = Field(
        default=5,
        ge=1,
        alias="BOOKING_FUNNEL_MEANINGFUL_STEP_SESSIONS",
    )
    barbershop_name: str = Field(default="Soul Cuts", alias="BARBERSHOP_NAME")
    upload_dir: str = Field(default="data/uploads", alias="UPLOAD_DIR")
    upload_url_prefix: str = Field(default="/media", alias="UPLOAD_URL_PREFIX")
    max_upload_size_bytes: int = Field(default=5 * 1024 * 1024, alias="MAX_UPLOAD_SIZE_BYTES")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("brevo_contact_list_id", mode="before")
    @classmethod
    def parse_optional_int(cls, value: str | int | None) -> int | None:
        if value == "":
            return None
        return value

    @field_validator("waitlist_quiet_hours_from", "waitlist_quiet_hours_to")
    @classmethod
    def validate_waitlist_quiet_hour(cls, value: str) -> str:
        try:
            hour, minute = (int(part) for part in value.split(":", maxsplit=1))
        except (TypeError, ValueError) as exc:
            raise ValueError("waitlist quiet hours must use HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("waitlist quiet hours must use HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("waitlist_offer_sms_template")
    @classmethod
    def validate_waitlist_offer_sms_template(cls, value: str) -> str:
        allowed = {
            "master_name",
            "appointment_date",
            "appointment_time",
            "hold_minutes",
            "booking_link",
        }
        variables = {name for _, name, _, _ in Formatter().parse(value) if name}
        unknown = variables - allowed
        if unknown:
            raise ValueError(f"Unknown waitlist SMS template variables: {', '.join(sorted(unknown))}")
        if "booking_link" not in variables:
            raise ValueError("WAITLIST_OFFER_SMS_TEMPLATE must include {booking_link}")
        return value

    @model_validator(mode="after")
    def validate_production_safety(self) -> "Settings":
        if self.app_env != "production":
            return self

        if self.debug:
            raise ValueError("DEBUG must be false when APP_ENV=production")

        normalized_secret = self.secret_key.strip()
        if len(normalized_secret) < 32 or normalized_secret in {
            "change-me",
            "change-me-to-a-long-random-string",
            "change-me-use-a-long-random-secret",
        }:
            raise ValueError("SECRET_KEY must be a non-default secret of at least 32 characters in production")

        local_origins = [
            origin
            for origin in self.cors_origins
            if "localhost" in origin or "127.0.0.1" in origin
        ]
        if local_origins:
            raise ValueError("CORS_ORIGINS must not contain localhost origins in production")

        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_uri(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_sync_database_uri(self) -> str:
        uri = self.sqlalchemy_database_uri
        if uri.startswith("postgresql+asyncpg://"):
            return uri.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        if uri.startswith("postgresql://"):
            return uri.replace("postgresql://", "postgresql+psycopg2://", 1)
        return uri


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
