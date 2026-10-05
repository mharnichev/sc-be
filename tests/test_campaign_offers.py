from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.models.booking import Booking, BookingStatus
from app.services.campaign_offers import CampaignOfferService, token_hash
from app.services.promotion import PromotionService


class Result:
    def scalar_one_or_none(self):
        return None


class ReadOnlySession:
    async def get(self, model, identifier):
        return SimpleNamespace(is_active=True)

    async def execute(self, stmt):
        return Result()


@pytest.mark.anyio
async def test_offer_quote_800_to_560_no_reservation(monkeypatch):
    service = CampaignOfferService()
    now = datetime.now(UTC)
    offer = SimpleNamespace(id=7, customer_id=2, master_id=4, allowed_service_ids=[9], starts_at=now-timedelta(days=1), expires_at=now+timedelta(days=5),
        consumed_booking_id=None, campaign_id=1, run_id=3, recipient_id=5, promotion_id=6,
        promotion_name_uk="Новий майстер", promotion_name_en="New Master", discount_percent=30)
    async def resolve(*args, **kwargs):
        assert kwargs['lock'] is False
        return offer
    async def eligible(*args, **kwargs):
        return None
    monkeypatch.setattr(service, 'resolve', resolve)
    monkeypatch.setattr(service, 'current_restriction', eligible)
    booking = Booking(master_id=4, start_at=now+timedelta(days=1))
    await service.apply(ReadOnlySession(), booking=booking, customer=SimpleNamespace(id=2),
        services=[SimpleNamespace(id=9, price=800)], token='secret', reserve=False)
    assert booking.total_amount == 560
    assert booking.promotion_discount_amount == 240
    assert booking.campaign_offer_reservation_id is None


@pytest.mark.anyio
@pytest.mark.parametrize('customer,master,services', [(3,4,[9]), (2,8,[9]), (2,4,[10])])
async def test_offer_rejects_identity_master_service(monkeypatch, customer, master, services):
    service = CampaignOfferService()
    offer = SimpleNamespace(customer_id=2, master_id=4, allowed_service_ids=[9])
    async def resolve(*args, **kwargs):
        return offer
    monkeypatch.setattr(service, 'resolve', resolve)
    with pytest.raises(HTTPException):
        await service.apply(ReadOnlySession(), booking=Booking(master_id=master), customer=SimpleNamespace(id=customer),
            services=[SimpleNamespace(id=i,price=800) for i in services], token='secret',reserve=False)


@pytest.mark.anyio
async def test_offer_context_reports_inactive_catalog_as_unavailable(monkeypatch):
    service = CampaignOfferService()
    now = datetime.now(UTC)
    offer = SimpleNamespace(
        id=7, master_id=4, allowed_service_ids=[9], discount_percent=30,
        promotion_name_uk="Новий майстер", promotion_name_en="New Master",
        starts_at=now - timedelta(days=1), expires_at=now + timedelta(days=5),
        consumed_booking_id=None, raw_link_requests=0,
    )

    async def resolve(*args, **kwargs):
        return offer

    async def unavailable(*args, **kwargs):
        return "offer_master_unavailable"

    async def eligible(*args, **kwargs):
        raise AssertionError("Audience eligibility should not be queried after catalog rejection")

    monkeypatch.setattr(service, "resolve", resolve)
    monkeypatch.setattr(service, "catalog_restriction", unavailable)
    monkeypatch.setattr(service, "current_restriction", eligible)

    context = await service.context(ReadOnlySession(), "secret")

    assert context["entitlement"] == "unavailable"
    assert offer.raw_link_requests == 1


def test_offer_promotion_not_public_and_hash_no_pii():
    assert not PromotionService().should_show_in_public_catalog(SimpleNamespace(recipient_offer_only=True))
    assert len(token_hash('secret')) == 64
    assert token_hash('secret') != token_hash('other')


@pytest.fixture
def anyio_backend():
    return 'asyncio'
