"""Owner-authorized Shop checkout; all views are local and never poll providers."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_optional_current_customer
from app.schemas.shop_online_payment import OnlinePaymentCommand, OnlinePaymentMethodCommand, OnlinePaymentRecovery, OnlinePaymentState
from app.services import shop_online_payment as service

class PrivatePaymentRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store", "Pragma": "no-cache"}
                raise
            except RequestValidationError as error:
                response = JSONResponse(status_code=422, content={"detail": jsonable_encoder(error.errors())})
            private_response(response)
            return response
        return handler


public_router = APIRouter(prefix="/orders", route_class=PrivatePaymentRoute)
backoffice_router = APIRouter(prefix="/shop-payments", route_class=PrivatePaymentRoute)


def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def customer_auth(request, customer):
    if customer is None and request.headers.get("authorization") is not None:
        raise HTTPException(401, "Invalid or expired customer authentication")


@public_router.get("/online-payment-capabilities")
async def capabilities(response: Response, session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    return await service.capabilities(session)


@public_router.get("/{order_id}/online-payment", response_model=OnlinePaymentState)
async def read(order_id: int, request: Request, response: Response,
        token: str | None = Header(default=None, alias="X-Order-Payment-Token"),
        customer=Depends(get_optional_current_customer), session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    customer_auth(request, customer)
    return await service.read(session, order_id, customer, token)


@public_router.post("/{order_id}/online-payment", response_model=OnlinePaymentState)
async def start(order_id: int, payload: OnlinePaymentCommand, request: Request, response: Response,
        key: str = Header(alias="Idempotency-Key"), token: str | None = Header(default=None, alias="X-Order-Payment-Token"),
        customer=Depends(get_optional_current_customer), session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    customer_auth(request, customer)
    return await service.start(session, order_id, customer, token, key)


@public_router.post("/{order_id}/online-payment/change-method", response_model=OnlinePaymentState)
async def change_method(order_id: int, payload: OnlinePaymentMethodCommand, request: Request, response: Response,
        key: str = Header(alias="Idempotency-Key"), token: str | None = Header(default=None, alias="X-Order-Payment-Token"),
        customer=Depends(get_optional_current_customer), session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    customer_auth(request, customer)
    return await service.change_method(session, order_id, customer, token, payload, key)


@backoffice_router.post("/orders/{order_id}/online-payment/recover", response_model=OnlinePaymentState)
async def recover(order_id: int, payload: OnlinePaymentRecovery, response: Response,
        key: str = Header(alias="Idempotency-Key"), actor=Depends(get_current_admin_user),
        session: AsyncSession = Depends(get_db_session)):
    private_response(response)
    return await service.recover(session, actor, order_id, payload, key)
