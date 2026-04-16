from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.security import get_token_scope, get_token_subject
from app.models.admin_user import AdminUser
from app.models.customer import Customer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/backoffice/auth/login")
customer_bearer_scheme = HTTPBearer(auto_error=False)


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_admin_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUser:
    subject = get_token_subject(token)
    scope = get_token_scope(token)
    if not subject or (scope is not None and scope != "admin"):
        raise _credentials_exception()

    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise _credentials_exception()

    user = await session.get(AdminUser, user_id)
    if not user or not user.is_active:
        raise _credentials_exception()
    return user


async def get_current_customer(
    credentials: HTTPAuthorizationCredentials | None = Depends(customer_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> Customer:
    if credentials is None:
        raise _credentials_exception()

    token = credentials.credentials
    subject = get_token_subject(token)
    scope = get_token_scope(token)
    if not subject or scope != "customer":
        raise _credentials_exception()

    try:
        customer_id = int(subject)
    except (TypeError, ValueError):
        raise _credentials_exception()

    customer = await session.get(Customer, customer_id)
    if not customer or not customer.is_active:
        raise _credentials_exception()
    return customer
