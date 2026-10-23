from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_optional_current_customer
from app.models.customer import Customer
from app.schemas.checkout_reminder import (CheckoutReminderCheckpoint, CheckoutReminderRecovery,
    CheckoutReminderRecoveryResponse)
from app.services import checkout_reminders as service
from app.services.shop_ingress_limits import admit_shop_ingress

public_router = APIRouter(prefix="/checkout-reminders")


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


async def source_admission(request: Request):
    await admit_shop_ingress(request, "reminder")


async def optional_customer(request: Request, customer=Depends(get_optional_current_customer)):
    if customer is None and request.headers.get("authorization") is not None:
        raise HTTPException(401, detail="Invalid or expired customer authentication")
    return customer


@public_router.get("/capabilities")
async def capabilities(response: Response, customer: Customer | None = Depends(optional_customer)):
    private_response(response)
    return service.capabilities(customer)


@public_router.post("/checkpoint", dependencies=[Depends(source_admission)])
async def checkpoint(payload: CheckoutReminderCheckpoint, response: Response,
    key: str = Header(alias="X-Checkout-Key", min_length=32, max_length=128),
    revision: int | None = Header(default=None, alias="X-Checkout-Revision", ge=1, le=2147483647),
    customer: Customer | None = Depends(optional_customer), session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    return await service.checkpoint(session, key, payload, customer, revision=revision)


@public_router.post("/cancel", dependencies=[Depends(source_admission)])
async def cancel(response: Response, key: str = Header(alias="X-Checkout-Key", min_length=32, max_length=128),
    revision: int | None = Header(default=None, alias="X-Checkout-Revision", ge=1, le=2147483647),
    customer: Customer | None = Depends(optional_customer), session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    return await service.cancel(session, key, customer, revision=revision)


@public_router.post("/recover", response_model=CheckoutReminderRecoveryResponse, dependencies=[Depends(source_admission)])
async def recover(payload: CheckoutReminderRecovery, response: Response,
    customer: Customer | None = Depends(optional_customer), session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    return await service.recover(session, payload.token, customer)
