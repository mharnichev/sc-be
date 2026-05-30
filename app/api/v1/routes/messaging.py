from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep
from app.models.booking import Booking
from app.models.customer import Customer
from app.models.messaging import (
    Campaign,
    CampaignAudienceFilter,
    CampaignStatus,
    CampaignType,
    ClientCommunicationPreference,
    ConsentStatus,
    MessageChannel,
    MessageLog,
    MessagePurpose,
    MessageRecipient,
    MessageTemplate,
    ReviewRequest,
)
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.messaging import (
    AudienceCriteria,
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
    ClientCommunicationPreferenceUpdate,
    MessageLogResponse,
    MessageRecipientResponse,
    MessageTemplateCreate,
    MessageTemplateResponse,
    MessageTemplateUpdate,
    MessagingAnalyticsResponse,
    RenderPreviewRequest,
    RenderPreviewResponse,
    StartCampaignRequest,
    TestMessageRequest,
)
from app.services.messaging import MessagingService, TelegramMessageProvider

backoffice_router = APIRouter()
service = MessagingService()
campaign_repo = BaseRepository(Campaign)
template_repo = BaseRepository(MessageTemplate)
recipient_repo = BaseRepository(MessageRecipient)
log_repo = BaseRepository(MessageLog)


class AudienceRequest(BaseModel):
    rules: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=1000)


class CampaignStatusUpdate(BaseModel):
    status: CampaignStatus


class ManualCustomerMessageRequest(BaseModel):
    body: str = Field(min_length=1)
    channel: str = "telegram"


def campaign_response(campaign: Campaign) -> CampaignResponse:
    data = CampaignResponse.model_validate(campaign)
    data.audience = service.audience_from_campaign(campaign)
    if campaign.template is not None:
        data.template_name = campaign.template.name
        data.template_body = campaign.template.body
    return data


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rules_to_audience(rules: list[dict[str, Any]], limit: int | None = None) -> AudienceCriteria:
    criteria: dict[str, Any] = {
        "all_clients": False,
        "barber_ids": [],
        "service_ids": [],
    }
    for rule in rules:
        rule_type = rule.get("type")
        if rule_type == "all_clients":
            criteria["all_clients"] = True
        elif rule_type == "selected_barber" and rule.get("barber_id"):
            criteria["barber_ids"].append(int(rule["barber_id"]))
        elif rule_type == "selected_service" and rule.get("service_id"):
            criteria["service_ids"].append(int(rule["service_id"]))
        elif rule_type == "visited_date_range":
            criteria["visited_from"] = _parse_datetime(rule.get("date_from"))
            criteria["visited_to"] = _parse_datetime(rule.get("date_to"))
        elif rule_type == "inactive_clients" and rule.get("inactive_days"):
            criteria["inactive_days"] = int(rule["inactive_days"])
        elif rule_type == "first_time_clients":
            criteria["first_time_clients"] = True
        elif rule_type == "vip_clients":
            criteria["vip_clients"] = True
        elif rule_type == "birthday_this_month":
            criteria["birthday_month"] = datetime.now().month
    if limit is not None:
        criteria["limit"] = limit
    if not criteria["all_clients"] and not any(
        [
            criteria["barber_ids"],
            criteria["service_ids"],
            criteria.get("visited_from"),
            criteria.get("visited_to"),
            criteria.get("inactive_days"),
            criteria.get("first_time_clients"),
            criteria.get("vip_clients"),
            criteria.get("birthday_month"),
        ]
    ):
        criteria["all_clients"] = True
    return AudienceCriteria.model_validate(criteria)


def _temporary_campaign(audience: AudienceCriteria, purpose: MessagePurpose = MessagePurpose.marketing) -> Campaign:
    campaign = Campaign(
        name="Audience preview",
        type=CampaignType.manual,
        status=CampaignStatus.draft,
        channel=MessageChannel.telegram,
        purpose=purpose,
        timezone="Europe/Kyiv",
    )
    campaign.audience_filter = CampaignAudienceFilter(criteria=audience.model_dump(mode="json", exclude_none=True))
    return campaign


async def _preference_map(session: AsyncSession, customer_ids: list[int]) -> dict[int, ClientCommunicationPreference]:
    if not customer_ids:
        return {}
    rows = (
        await session.execute(
            select(ClientCommunicationPreference).where(ClientCommunicationPreference.customer_id.in_(customer_ids))
        )
    ).scalars().all()
    return {item.customer_id: item for item in rows}


def _customer_name(customer: Customer) -> str:
    return " ".join(part for part in [customer.name, customer.surname] if part).strip() or customer.phone


async def _audience_estimate(session: AsyncSession, customers: list[Customer]) -> dict[str, int]:
    preferences = await _preference_map(session, [customer.id for customer in customers])
    missing_chat_id = 0
    opted_out = 0
    eligible = 0
    for customer in customers:
        preference = preferences.get(customer.id)
        allowed, _ = service.communication_allowed(preference, MessagePurpose.marketing)
        if not preference or not preference.telegram_chat_id:
            missing_chat_id += 1
        if preference and (preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out):
            opted_out += 1
        if allowed and preference and preference.telegram_chat_id:
            eligible += 1
    return {
        "total": len(customers),
        "eligible": eligible,
        "missing_chat_id": missing_chat_id,
        "opted_out": opted_out,
        "excluded": len(customers) - eligible,
    }


@backoffice_router.post("/templates", response_model=MessageTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_message_template(
    payload: MessageTemplateCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.create_template(session, payload.model_dump())
    return MessageTemplateResponse.model_validate(template)


@backoffice_router.get("/templates", response_model=PaginatedResponse[MessageTemplateResponse])
async def list_message_templates(
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MessageTemplateResponse]:
    stmt = select(MessageTemplate).order_by(MessageTemplate.created_at.desc())
    items, total = await template_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MessageTemplateResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("/templates/{template_id}/duplicate", response_model=MessageTemplateResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_message_template(
    template_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.get_template(session, template_id)
    clone = await service.create_template(
        session,
        {
            "name": f"{template.name} copy {int(datetime.now().timestamp())}",
            "channel": template.channel,
            "language": template.language,
            "body": template.body,
            "is_active": template.is_active,
        },
    )
    return MessageTemplateResponse.model_validate(clone)


@backoffice_router.get("/templates/{template_id}", response_model=MessageTemplateResponse)
async def get_message_template(
    template_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.get_template(session, template_id)
    return MessageTemplateResponse.model_validate(template)


@backoffice_router.put("/templates/{template_id}", response_model=MessageTemplateResponse)
async def update_message_template(
    template_id: int,
    payload: MessageTemplateUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.get_template(session, template_id)
    updated = await service.update_template(session, template, payload.model_dump(exclude_unset=True))
    return MessageTemplateResponse.model_validate(updated)


@backoffice_router.patch("/templates/{template_id}", response_model=MessageTemplateResponse)
async def patch_message_template(
    template_id: int,
    payload: MessageTemplateUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    return await update_message_template(template_id, payload, _, session)


@backoffice_router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message_template(
    template_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    template = await service.get_template(session, template_id)
    await template_repo.delete(session, template)


@backoffice_router.get("/dashboard")
async def get_messaging_dashboard(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    analytics = await service.analytics(session)
    status_counts = dict(
        (
            await session.execute(
                select(Campaign.status, func.count(Campaign.id)).group_by(Campaign.status)
            )
        ).all()
    )
    recent_campaigns = (
        await session.execute(select(Campaign).order_by(Campaign.updated_at.desc()).limit(5))
    ).scalars().all()
    return {
        "active_campaigns": status_counts.get(CampaignStatus.active, 0),
        "scheduled_campaigns": 0,
        "messages_sent": analytics["sent_count"],
        "failed_messages": analytics["failed_count"],
        "delivery_rate": analytics["delivery_rate"],
        "review_requests_sent": analytics["review_request_sent_count"],
        "recent_activity": [
            {
                "id": campaign.id,
                "campaign_id": campaign.id,
                "title": campaign.name,
                "description": f"{campaign.channel.value} · {campaign.type.value}",
                "status": campaign.status.value,
                "created_at": campaign.updated_at,
            }
            for campaign in recent_campaigns
        ],
    }


@backoffice_router.get("/settings")
async def get_messaging_settings(
    _: object = Depends(get_current_admin_user),
) -> dict[str, Any]:
    return {
        "telegram_bot_status": "connected" if settings.telegram_bot_token else "offline",
        "default_review_links": {
            "google": settings.messaging_default_review_url or "",
            "instagram": "",
            "internal": "",
            "custom": "",
        },
        "default_template_ids": {campaign_type.value: None for campaign_type in CampaignType},
        "quiet_hours_from": "21:00",
        "quiet_hours_to": "09:00",
        "default_rate_limit": settings.messaging_batch_size,
        "default_timezone": "Europe/Kyiv",
        "opt_out_text": "Напишіть STOP, щоб відписатися.",
        "test_recipient_chat_id": None,
        "multi_location_enabled": False,
    }


@backoffice_router.patch("/settings")
@backoffice_router.put("/settings")
async def update_messaging_settings(
    payload: dict[str, Any],
    _: object = Depends(get_current_admin_user),
) -> dict[str, Any]:
    settings_payload = await get_messaging_settings(_)
    settings_payload.update(payload)
    return settings_payload


@backoffice_router.post("/audience/estimate")
async def estimate_messaging_audience(
    payload: AudienceRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    audience = _rules_to_audience(payload.rules)
    campaign = _temporary_campaign(audience)
    customers = list(await service.calculate_recipients(session, campaign))
    return await _audience_estimate(session, customers)


@backoffice_router.post("/audience/preview")
async def preview_messaging_recipients(
    payload: AudienceRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    audience = _rules_to_audience(payload.rules, payload.limit)
    campaign = _temporary_campaign(audience)
    customers = list(await service.calculate_recipients(session, campaign))
    preferences = await _preference_map(session, [customer.id for customer in customers])
    rows = []
    for customer in customers:
        preference = preferences.get(customer.id)
        allowed, reason = service.communication_allowed(preference, MessagePurpose.marketing)
        rows.append(
            {
                "id": customer.id,
                "name": _customer_name(customer),
                "phone": customer.phone,
                "telegram_chat_id": preference.telegram_chat_id if preference else None,
                "marketing_consent": bool(preference and preference.marketing_consent == ConsentStatus.opted_in),
                "opt_out": bool(preference and (preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out)),
                "preferred_language": preference.preferred_language if preference else None,
                "eligible": bool(allowed and preference and preference.telegram_chat_id),
                "exclusion_reason": None if allowed else reason,
            }
        )
    return rows


@backoffice_router.post("/campaigns", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    data = payload.model_dump(exclude={"audience"})
    campaign = await service.create_campaign(session, data, payload.audience)
    return campaign_response(campaign)


@backoffice_router.get("/campaigns", response_model=PaginatedResponse[CampaignResponse])
async def list_campaigns(
    pagination: PaginationDep,
    status_filter: CampaignStatus | None = Query(default=None, alias="status"),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CampaignResponse]:
    stmt = (
        select(Campaign)
        .options(selectinload(Campaign.audience_filter), selectinload(Campaign.template))
        .order_by(Campaign.created_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(Campaign.status == status_filter)
    items, total = await campaign_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[campaign_response(item) for item in items],
    )


@backoffice_router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    return campaign_response(campaign)


@backoffice_router.put("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    data = payload.model_dump(exclude_unset=True, exclude={"audience"})
    updated = await service.update_campaign(session, campaign, data, payload.audience)
    return campaign_response(updated)


@backoffice_router.patch("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def patch_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    return await update_campaign(campaign_id, payload, _, session)


@backoffice_router.post("/campaigns/{campaign_id}/duplicate", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    clone = await service.create_campaign(
        session,
        {
            "name": f"{campaign.name} copy",
            "type": campaign.type,
            "status": CampaignStatus.draft,
            "channel": campaign.channel,
            "purpose": campaign.purpose,
            "template_id": campaign.template_id,
            "scheduled_at": campaign.scheduled_at,
            "timezone": campaign.timezone,
            "review_delay_minutes": campaign.review_delay_minutes,
            "follow_up_delay_days": campaign.follow_up_delay_days,
            "review_platform": campaign.review_platform,
            "review_url": campaign.review_url,
            "discount_code": campaign.discount_code,
            "location_key": campaign.location_key,
            "metadata_json": campaign.metadata_json,
        },
        service.audience_from_campaign(campaign),
    )
    return campaign_response(clone)


@backoffice_router.patch("/campaigns/{campaign_id}/status", response_model=CampaignResponse)
async def update_campaign_status(
    campaign_id: int,
    payload: CampaignStatusUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    campaign.status = payload.status
    await session.commit()
    return campaign_response(await service.get_campaign(session, campaign_id))


@backoffice_router.post("/campaigns/{campaign_id}/send-test")
async def send_campaign_test_message(
    campaign_id: int,
    payload: dict[str, str | None],
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    campaign = await service.get_campaign(session, campaign_id)
    chat_id = payload.get("recipient") or payload.get("chat_id")
    if not chat_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="recipient is required")
    body = campaign.template.body if campaign.template is not None else campaign.metadata_json.get("message_body")
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Campaign has no message body")
    result = await TelegramMessageProvider().send_message(destination=chat_id, body=body)
    return {"sent": True, "provider_message_id": result.provider_message_id, "provider_response": result.raw_response}


@backoffice_router.delete("/campaigns/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    campaign = await service.get_campaign(session, campaign_id)
    await campaign_repo.delete(session, campaign)


@backoffice_router.post("/campaigns/{campaign_id}/enable", response_model=CampaignResponse)
async def enable_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    campaign.status = CampaignStatus.active
    await session.commit()
    return campaign_response(await service.get_campaign(session, campaign_id))


@backoffice_router.post("/campaigns/{campaign_id}/disable", response_model=CampaignResponse)
async def disable_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    campaign.status = CampaignStatus.paused
    await session.commit()
    return campaign_response(await service.get_campaign(session, campaign_id))


@backoffice_router.post("/campaigns/{campaign_id}/pause", response_model=CampaignResponse)
async def pause_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    return await disable_campaign(campaign_id, _, session)


@backoffice_router.post("/campaigns/{campaign_id}/resume", response_model=CampaignResponse)
async def resume_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    return await enable_campaign(campaign_id, _, session)


@backoffice_router.post("/campaigns/{campaign_id}/start")
async def start_manual_campaign(
    campaign_id: int,
    payload: StartCampaignRequest,
    background_tasks: BackgroundTasks,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int | str]:
    campaign = await service.get_campaign(session, campaign_id)
    if campaign.status not in {CampaignStatus.active, CampaignStatus.draft}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only draft or active campaigns can be started")
    campaign.status = CampaignStatus.active
    enqueued = await service.enqueue_campaign_recipients(session, campaign, payload.scheduled_at)
    background_tasks.add_task(_process_pending_messages_background)
    return {"campaign_id": campaign_id, "enqueued": enqueued, "status": campaign.status.value}


async def _process_pending_messages_background() -> None:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as background_session:
        await service.process_pending_messages(background_session)


@backoffice_router.get("/campaigns/{campaign_id}/recipients", response_model=PaginatedResponse[MessageRecipientResponse])
async def get_campaign_recipients(
    campaign_id: int,
    pagination: PaginationDep,
    calculate: bool = Query(default=False),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MessageRecipientResponse]:
    campaign = await service.get_campaign(session, campaign_id)
    if calculate:
        customers = await service.calculate_recipients(session, campaign)
        items = [
            MessageRecipientResponse(
                id=0,
                campaign_id=campaign_id,
                customer_id=customer.id,
                appointment_id=None,
                channel=campaign.channel,
                status="pending",
                idempotency_key=service.build_idempotency_key(campaign_id, customer.id),
                scheduled_at=campaign.scheduled_at,
                sent_at=None,
                rendered_message=None,
                attempts=0,
                next_retry_at=None,
                last_error=None,
                provider_message_id=None,
                created_at=campaign.created_at,
                updated_at=campaign.updated_at,
            )
            for customer in customers[(pagination.page - 1) * pagination.page_size : pagination.page * pagination.page_size]
        ]
        return PaginatedResponse(total=len(customers), page=pagination.page, page_size=pagination.page_size, items=items)

    stmt = (
        select(MessageRecipient)
        .where(MessageRecipient.campaign_id == campaign_id)
        .order_by(MessageRecipient.created_at.desc())
    )
    recipients, total = await recipient_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MessageRecipientResponse.model_validate(item) for item in recipients],
    )


@backoffice_router.get("/campaigns/{campaign_id}/logs", response_model=PaginatedResponse[MessageLogResponse])
async def get_campaign_logs(
    campaign_id: int,
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MessageLogResponse]:
    await service.get_campaign(session, campaign_id)
    stmt = select(MessageLog).where(MessageLog.campaign_id == campaign_id).order_by(MessageLog.created_at.desc())
    logs, total = await log_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MessageLogResponse.model_validate(item) for item in logs],
    )


@backoffice_router.post("/campaigns/{campaign_id}/retry-failed")
async def retry_failed_campaign_messages(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    await service.get_campaign(session, campaign_id)
    return {"queued": await service.retry_failed(session, campaign_id)}


@backoffice_router.get("/analytics", response_model=MessagingAnalyticsResponse)
async def get_messaging_analytics(
    campaign_id: int | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessagingAnalyticsResponse:
    return MessagingAnalyticsResponse.model_validate(await service.analytics(session, campaign_id))


@backoffice_router.post("/preview", response_model=RenderPreviewResponse)
async def preview_message(
    payload: RenderPreviewRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> RenderPreviewResponse:
    customer = await session.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    campaign = await service.get_campaign(session, payload.campaign_id) if payload.campaign_id else None
    appointment = None
    if payload.appointment_id is not None:
        appointment = (
            await session.execute(
                select(Booking)
                .options(selectinload(Booking.master), selectinload(Booking.service))
                .where(Booking.id == payload.appointment_id)
            )
        ).scalar_one_or_none()
        if appointment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    if payload.body is not None:
        body = payload.body
    elif payload.template_id is not None:
        body = (await service.get_template(session, payload.template_id)).body
    elif campaign and campaign.template:
        body = campaign.template.body
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No template body available")
    rendered, variables = await service.render_for_customer(
        session,
        body,
        customer,
        campaign,
        appointment,
        payload.extra_variables,
    )
    return RenderPreviewResponse(rendered_message=rendered, variables=variables)


@backoffice_router.post("/test-message")
async def send_test_message(
    payload: TestMessageRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    body = payload.body
    campaign = await service.get_campaign(session, payload.campaign_id) if payload.campaign_id else None
    if body is None and payload.template_id is not None:
        body = (await service.get_template(session, payload.template_id)).body
    if body is None and campaign is not None and campaign.template is not None:
        body = campaign.template.body
    if body is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body, template_id or campaign_id is required")
    customer = await session.get(Customer, payload.customer_id) if payload.customer_id else None
    if customer is not None:
        body, _ = await service.render_for_customer(session, body, customer, campaign)
    else:
        service.validate_template_body(body)
    result = await TelegramMessageProvider().send_message(destination=payload.chat_id, body=body)
    return {"sent": True, "provider_message_id": result.provider_message_id, "provider_response": result.raw_response}


@backoffice_router.get("/customers/{customer_id}/preferences")
async def get_customer_communication_preferences(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    preference = await service.get_preference(session, customer_id)
    logs = (
        await session.execute(
            select(MessageLog).where(MessageLog.customer_id == customer_id).order_by(MessageLog.created_at.desc()).limit(20)
        )
    ).scalars().all()
    review_requests = (
        await session.execute(
            select(ReviewRequest)
            .where(ReviewRequest.customer_id == customer_id)
            .order_by(ReviewRequest.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "telegram_chat_id": preference.telegram_chat_id if preference else None,
        "telegram_status": "connected" if preference and preference.telegram_chat_id else "missing",
        "marketing_consent": bool(preference and preference.marketing_consent == ConsentStatus.opted_in),
        "opt_out": bool(preference and (preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out)),
        "preferred_language": (preference.preferred_language if preference else None) or "uk",
        "message_history": [
            {
                "id": log.id,
                "client_id": customer_id,
                "client_name": _customer_name(customer),
                "phone": customer.phone,
                "telegram_status": log.status.value,
                "sent_at": log.created_at,
                "failure_reason": log.error_reason,
            }
            for log in logs
        ],
        "review_requests": [
            {
                "id": item.id,
                "client_id": customer_id,
                "client_name": _customer_name(customer),
                "phone": customer.phone,
                "telegram_status": "sent" if item.sent_at else "queued",
                "sent_at": item.sent_at,
                "failure_reason": None,
            }
            for item in review_requests
        ],
    }


@backoffice_router.post("/customers/{customer_id}/messages")
async def send_customer_manual_message(
    customer_id: int,
    payload: ManualCustomerMessageRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    preference = await service.get_preference(session, customer_id)
    if payload.channel != "telegram":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only Telegram is currently supported")
    if preference is None or not preference.telegram_chat_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer has no Telegram chat_id")
    result = await TelegramMessageProvider().send_message(destination=preference.telegram_chat_id, body=payload.body)
    return {"sent": True, "provider_message_id": result.provider_message_id, "provider_response": result.raw_response}


@backoffice_router.put("/customers/{customer_id}/preferences")
async def update_customer_communication_preferences(
    customer_id: int,
    payload: ClientCommunicationPreferenceUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    preference = await service.upsert_preference(session, customer_id, payload.model_dump(exclude_unset=True))
    return {
        "customer_id": preference.customer_id,
        "telegram_chat_id": preference.telegram_chat_id,
        "preferred_language": preference.preferred_language,
        "marketing_consent": preference.marketing_consent == ConsentStatus.opted_in,
        "transactional_consent": preference.transactional_consent.value,
        "do_not_contact": preference.do_not_contact,
        "opt_out": preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out,
        "opted_out_at": preference.opted_out_at,
    }


@backoffice_router.patch("/customers/{customer_id}/preferences")
async def patch_customer_communication_preferences(
    customer_id: int,
    payload: ClientCommunicationPreferenceUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await update_customer_communication_preferences(customer_id, payload, _, session)


@backoffice_router.post("/jobs/process-pending")
async def process_pending_messages(
    limit: int | None = Query(default=None, ge=1, le=1000),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    return {"processed": await service.process_pending_messages(session, limit)}


@backoffice_router.post("/jobs/create-review-requests")
async def create_review_requests(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    return {"created": await service.create_review_requests_for_completed_appointments(session)}
