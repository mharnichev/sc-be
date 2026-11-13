from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.security import create_scoped_access_token, get_token_scope, get_token_subject
from app.models.admin_user import AdminUser
from app.models.shop_balance import ShopBalanceEntry
from app.services.auth import AuthService
from app.services.shop_ingress_limits import admit_balance_login

ALLOWED_EMAILS = {
    "admin.generated@example.com",
    "admin.valery@gmail.com",
    "admin.gleb@gmail.com",
}
KYIV = ZoneInfo("Europe/Kyiv")
Kind = Literal["income", "expense"]

router = APIRouter(prefix="/shop-balance")
BALANCE_SCOPE = "shop-balance"
balance_bearer = OAuth2PasswordBearer(tokenUrl="/api/v1/shop-balance/login")
auth_service = AuthService()
Session = Annotated[AsyncSession, Depends(get_db_session)]


class EntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: Kind
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    amount_minor: Annotated[int, Field(strict=True, ge=1, le=999_999_999_999)]


class EntryResponse(EntryInput):
    model_config = ConfigDict(from_attributes=True)

    author_email: str
    created_at: datetime


def check_balance_email(user: AdminUser) -> AdminUser:
    if user.email.casefold() not in ALLOWED_EMAILS:
        raise HTTPException(403, "Shop balance access denied")
    return user


async def require_balance_access(
    token: Annotated[str, Depends(balance_bearer)], session: Session,
) -> AdminUser:
    subject = get_token_subject(token)
    if (
        get_token_scope(token) == BALANCE_SCOPE
        and subject and len(subject) <= 10 and subject.isascii() and subject.isdecimal()
        and 0 < int(subject) <= 2_147_483_647
    ):
        user = await session.get(AdminUser, int(subject))
        if user is not None and user.is_active:
            return check_balance_email(user)
    raise HTTPException(401, "Invalid shop balance session", headers={"WWW-Authenticate": "Bearer"})


BalanceUser = Annotated[AdminUser, Depends(require_balance_access)]


class ShopBalanceToken(BaseModel):
    access_token: str


@router.post("/login", response_model=ShopBalanceToken)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: Session,
    request: Request,
    response: Response,
) -> ShopBalanceToken:
    email = form.username.casefold()
    await admit_balance_login(request, email if email in ALLOWED_EMAILS else None)
    user = await auth_service.authenticate(session, email=form.username, password=form.password)
    check_balance_email(user)
    response.headers["Cache-Control"] = "no-store"
    return ShopBalanceToken(
        access_token=create_scoped_access_token(user.id, BALANCE_SCOPE, timedelta(minutes=15)),
    )


@router.get("/entries", response_model=list[EntryResponse])
async def list_entries(
    user: BalanceUser,
    session: Session,
    response: Response,
    month: Annotated[str, Query(pattern=r"^20[0-9]{2}-(0[1-9]|1[0-2])$")],
) -> list[ShopBalanceEntry]:
    year, month_number = map(int, month.split("-"))
    start = datetime(year, month_number, 1, tzinfo=KYIV)
    end = datetime(year + month_number // 12, month_number % 12 + 1, 1, tzinfo=KYIV)
    entries = await session.scalars(
        select(ShopBalanceEntry)
        .where(ShopBalanceEntry.created_at >= start, ShopBalanceEntry.created_at < end)
        .order_by(ShopBalanceEntry.created_at.desc(), ShopBalanceEntry.id.desc())
    )
    response.headers["Cache-Control"] = "no-store"
    return list(entries)


@router.post("/entries", response_model=EntryResponse)
async def create_entry(
    payload: EntryInput, user: BalanceUser, session: Session, response: Response,
) -> ShopBalanceEntry:
    statement = (
        insert(ShopBalanceEntry)
        .values(**payload.model_dump(), author_id=user.id, author_email=user.email)
        .on_conflict_do_nothing(index_elements=[ShopBalanceEntry.id])
        .returning(ShopBalanceEntry)
    )
    entry = (await session.scalars(statement)).one_or_none()
    if entry is None:
        entry = await session.get(ShopBalanceEntry, payload.id)
        if (
            entry is None
            or entry.author_id != user.id
            or (entry.kind, entry.description, entry.amount_minor)
            != (payload.kind, payload.description, payload.amount_minor)
        ):
            raise HTTPException(409, "Entry ID is already in use")
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return entry
