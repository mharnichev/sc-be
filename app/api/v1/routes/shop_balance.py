from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer, field_validator
from sqlalchemy import Date, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.security import create_scoped_access_token, get_token_scope, get_token_subject
from app.dependencies.auth import get_current_admin_user
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
backoffice_router = APIRouter(prefix="/shop-balance")
BALANCE_SCOPE = "shop-balance"
balance_bearer = OAuth2PasswordBearer(tokenUrl="/api/v1/shop-balance/login")
auth_service = AuthService()
Session = Annotated[AsyncSession, Depends(get_db_session)]


class EntryDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    amount_minor: Annotated[int, Field(strict=True, ge=1, le=999_999_999_999)]


class EntryEdit(EntryDetails):
    entry_date: date | None = None

    @field_validator("entry_date")
    @classmethod
    def validate_entry_date(cls, value: date | None) -> date | None:
        if value is not None and not 2000 <= value.year <= 2099:
            raise ValueError("Entry date must be between 2000 and 2099")
        return value


class EntryInput(EntryDetails):
    id: UUID


class EntryCreate(EntryEdit):
    id: UUID


class EntryResponse(EntryDetails):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    author_email: str
    created_at: datetime
    entry_date: date | None = None
    updated_at: datetime | None = None
    updated_by_email: str | None = None

    @field_serializer("description")
    def serialize_description(self, description: str) -> str:
        prefix = "[Історія · день невідомий]"
        if description.startswith(prefix + " "):
            return description.removeprefix(prefix).lstrip()
        return description


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


def require_balance_editor(user: Annotated[AdminUser, Depends(get_current_admin_user)]) -> AdminUser:
    return check_balance_email(user)


BalanceEditor = Annotated[AdminUser, Depends(require_balance_editor)]


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


@router.get("/entries", response_model=list[EntryResponse], dependencies=[Depends(require_balance_access)])
@backoffice_router.get("/entries", response_model=list[EntryResponse], dependencies=[Depends(require_balance_editor)])
async def list_entries(
    session: Session,
    response: Response,
    month: Annotated[str, Query(pattern=r"^20[0-9]{2}-(0[1-9]|1[0-2])$")],
) -> list[ShopBalanceEntry]:
    year, month_number = map(int, month.split("-"))
    start = date(year, month_number, 1)
    end = date(year + month_number // 12, month_number % 12 + 1, 1)
    entry_day = func.coalesce(
        ShopBalanceEntry.entry_date, cast(func.timezone(KYIV.key, ShopBalanceEntry.created_at), Date),
    )
    entries = await session.scalars(
        select(ShopBalanceEntry)
        .where(entry_day >= start, entry_day < end)
        .order_by(entry_day.desc(), ShopBalanceEntry.created_at.desc(), ShopBalanceEntry.id.desc())
    )
    response.headers["Cache-Control"] = "no-store"
    return list(entries)


@router.post("/entries", response_model=EntryResponse)
async def create_entry(
    payload: EntryInput, user: BalanceUser, session: Session, response: Response,
) -> ShopBalanceEntry:
    return await save_entry(payload, user, session, response)


@backoffice_router.post("/entries", response_model=EntryResponse)
async def create_backoffice_entry(
    payload: EntryCreate, user: BalanceEditor, session: Session, response: Response,
) -> ShopBalanceEntry:
    return await save_entry(payload, user, session, response, payload.entry_date)


async def save_entry(
    payload: EntryInput | EntryCreate, user: AdminUser, session: AsyncSession, response: Response,
    requested_date: date | None = None,
) -> ShopBalanceEntry:
    entry_date = requested_date or datetime.now(KYIV).date()
    statement = (
        insert(ShopBalanceEntry)
        .values(**payload.model_dump(exclude={"entry_date"}), entry_date=entry_date, author_id=user.id, author_email=user.email)
        .on_conflict_do_nothing(index_elements=[ShopBalanceEntry.id])
        .returning(ShopBalanceEntry)
    )
    entry = (await session.scalars(statement)).one_or_none()
    if entry is None:
        entry = await session.get(ShopBalanceEntry, payload.id)
        if (
            entry is None
            or entry.author_id != user.id
            or (
                requested_date is not None
                and requested_date != (entry.entry_date or entry.created_at.astimezone(KYIV).date())
            )
            or (entry.kind, entry.description, entry.amount_minor)
            != (payload.kind, payload.description, payload.amount_minor)
        ):
            raise HTTPException(409, "Entry ID is already in use")
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return entry



@backoffice_router.patch("/entries/{entry_id}", response_model=EntryResponse)
async def edit_entry(
    entry_id: UUID, payload: EntryEdit, user: BalanceEditor, session: Session, response: Response,
) -> ShopBalanceEntry:
    statement = (
        update(ShopBalanceEntry)
        .where(ShopBalanceEntry.id == entry_id)
        .values(**payload.model_dump(exclude_none=True), updated_at=datetime.now(UTC), updated_by_email=user.email)
        .returning(ShopBalanceEntry)
    )
    entry = (await session.scalars(statement)).one_or_none()
    if entry is None:
        raise HTTPException(404, "Shop balance entry not found")
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return entry


@backoffice_router.delete("/entries/{entry_id}", status_code=204)
async def delete_entry(entry_id: UUID, user: BalanceEditor, session: Session) -> Response:
    statement = (
        delete(ShopBalanceEntry)
        .where(ShopBalanceEntry.id == entry_id)
        .returning(ShopBalanceEntry.id)
    )
    if (await session.scalars(statement)).one_or_none() is None:
        raise HTTPException(404, "Shop balance entry not found")
    await session.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
