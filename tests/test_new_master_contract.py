from datetime import UTC, datetime
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from app.schemas.messaging import CampaignCreate, CampaignResponse, CampaignUpdate
from app.schemas.segment import SegmentRules
from app.utils.seed_new_master_campaign import RULES, DEFAULT_BODY
from app.services.messaging import MessagingService


def test_offer_campaign_defaults_and_legacy_rate_alias():
    value = CampaignCreate(name="Other master", type="manual", channel="sms", offer_master_id=9,
                           max_messages_per_minute=19)
    assert value.sending_window.start == "10:00"
    assert value.sending_window.end == "18:00"
    assert value.sending_window.days == list(range(7))
    assert value.sms_recipients_per_minute == 19
    assert value.marketing_max_contacts == 1
    assert value.offer_expires_at is None
    assert CampaignUpdate(max_messages_per_minute=20).sms_recipients_per_minute == 20


@pytest.mark.parametrize('data', [{'sending_window': None}, {'quiet_hours_start': '18:00'}])
def test_offer_campaign_critical_config_not_silently_ignored(data):
    with pytest.raises(ValidationError):
        CampaignCreate(name="Offer", type="manual", channel="sms", offer_master_id=4, **data)


def test_legacy_campaign_nullable_columns_read_metadata():
    now = datetime.now(UTC)
    campaign = SimpleNamespace(id=1, name="Old", type="manual", status="draft", channel="sms", purpose="marketing",
        template_id=None, scheduled_at=None, timezone="Europe/Kyiv", review_delay_minutes=None, follow_up_delay_days=None,
        review_platform=None, review_url=None, discount_code=None, location_key=None, created_at=now, updated_at=now,
        sms_recipients_per_minute=None, marketing_frequency_days=None, sending_window=None,
        metadata_json={"sms_recipients_per_minute":17, "marketing_frequency_days":14,
                       "sending_window":{"start":"11:00","end":"16:00","days":[0]}})
    result = CampaignResponse.model_validate(campaign)
    assert result.sms_recipients_per_minute == 17
    assert result.marketing_frequency_days == 14
    assert result.sending_window.start == "11:00"
    campaign.sms_recipients_per_minute = 20
    assert CampaignResponse.model_validate(campaign).sms_recipients_per_minute == 20


def test_seed_inclusive_calendar_bounds_and_template_provider_characters():
    rule = SegmentRules.model_validate(RULES)
    assert rule.conditions[0].min_inclusive and rule.conditions[0].max_inclusive
    assert rule.conditions[0].min == 3 and rule.conditions[0].max == 12
    assert rule.exclusions[0].type == "upcoming_booking"
    MessagingService().validate_template_body(DEFAULT_BODY, channel="sms")
