from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import asc, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_customer
from app.dependencies.common import PaginationDep, parse_optional_bool_query
from app.models.customer import Customer
from app.models.order import Order
from app.repositories.base import BaseRepository
from app.schemas.auth import (
    CustomerCreate,
    CustomerAuthResponse,
    CustomerOtpRequest,
    CustomerOtpRequestResponse,
    CustomerOtpVerifyRequest,
    CustomerResponse,
    CustomerSummaryResponse,
    CustomerUpdate,
)
from app.schemas.common import PaginatedResponse
from app.schemas.order import OrderSummaryResponse
from app.services.customer_auth import CustomerAuthService

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Customer)
orders_repo = BaseRepository(Order)
service = CustomerAuthService()


@public_router.post("/auth/request-otp", response_model=CustomerOtpRequestResponse, status_code=status.HTTP_202_ACCEPTED)
async def request_customer_otp(
    payload: CustomerOtpRequest,
    session: AsyncSession = Depends(get_db_session),
) -> CustomerOtpRequestResponse:
    result = await service.request_otp(session, payload.phone)
    return CustomerOtpRequestResponse(
        message="OTP code sent",
        expires_in_seconds=result.expires_in_seconds,
        retry_after_seconds=result.retry_after_seconds,
        sends_left_today=result.sends_left_today,
        debug_otp_code=result.debug_otp_code,
    )


@public_router.post("/auth/verify-otp", response_model=CustomerAuthResponse)
async def verify_customer_otp(
    payload: CustomerOtpVerifyRequest,
    session: AsyncSession = Depends(get_db_session),
) -> CustomerAuthResponse:
    result = await service.verify_otp(session, payload.phone, payload.otp_code)
    return CustomerAuthResponse(
        access_token=result.access_token,
        customer=CustomerResponse.model_validate(result.customer),
        is_new_customer=result.is_new_customer,
        attempts_left_today=result.attempts_left_today,
    )


@public_router.get("/me", response_model=CustomerResponse)
async def customer_me(current_customer: Customer = Depends(get_current_customer)) -> CustomerResponse:
    return CustomerResponse.model_validate(current_customer)


@public_router.patch("/me", response_model=CustomerResponse)
async def update_customer_me(
    payload: CustomerUpdate,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await service.update_customer(session, current_customer, payload.model_dump(exclude_unset=True))
    return CustomerResponse.model_validate(customer)


@backoffice_router.get("", response_model=PaginatedResponse[CustomerSummaryResponse])
async def backoffice_list_customers(
    pagination: PaginationDep,
    is_active: str | None = Query(default=None),
    is_verified: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CustomerSummaryResponse]:
    parsed_is_active = parse_optional_bool_query(is_active, "is_active")
    parsed_is_verified = parse_optional_bool_query(is_verified, "is_verified")

    sortable_fields = {
        "id": Customer.id,
        "phone": Customer.phone,
        "name": Customer.name,
        "surname": Customer.surname,
        "created_at": Customer.created_at,
        "last_login_at": Customer.last_login_at,
    }
    if sort_by not in sortable_fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid sort_by value")
    if sort_order not in {"asc", "desc"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid sort_order value")

    order_clause = asc(sortable_fields[sort_by]) if sort_order == "asc" else desc(sortable_fields[sort_by])
    stmt = select(Customer).order_by(order_clause, Customer.id.desc())
    if parsed_is_active is not None:
        stmt = stmt.where(Customer.is_active.is_(parsed_is_active))
    if parsed_is_verified is not None:
        if parsed_is_verified:
            stmt = stmt.where(Customer.phone_verified_at.is_not(None))
        else:
            stmt = stmt.where(Customer.phone_verified_at.is_(None))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Customer.phone.ilike(pattern),
                Customer.email.ilike(pattern),
                Customer.name.ilike(pattern),
                Customer.surname.ilike(pattern),
            )
        )

    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[CustomerSummaryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[CustomerSummaryResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/{customer_id}", response_model=CustomerResponse)
async def backoffice_get_customer(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return CustomerResponse.model_validate(customer)


@backoffice_router.get("/{customer_id}/orders", response_model=PaginatedResponse[OrderSummaryResponse])
async def backoffice_customer_orders(
    customer_id: int,
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[OrderSummaryResponse]:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
    )
    items, total = await orders_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[OrderSummaryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[OrderSummaryResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
async def backoffice_create_customer(
    payload: CustomerCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await service.create_customer(session, payload.model_dump())
    return CustomerResponse.model_validate(customer)


@backoffice_router.put("/{customer_id}", response_model=CustomerResponse)
async def backoffice_update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    updated = await service.update_customer(session, customer, payload.model_dump(exclude_unset=True))
    return CustomerResponse.model_validate(updated)


@backoffice_router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def backoffice_delete_customer(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    await service.delete_customer(session, customer)
