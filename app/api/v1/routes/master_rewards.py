"""Private manual rewards; route-level errors never echo inputs or payloads."""
from datetime import date
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.master_rewards import RewardOperation
from app.schemas.master_rewards import PolicyCommand, EntryCommand, ReviewCommand, SettlementPreview, SettlementCommand, PolicyView, EntryView, SettlementView, PreviewView
from app.schemas.common import PaginatedResponse
from app.services import master_rewards as service


class PrivateRewardRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse({'detail': {'code': 'invalid_command'}}, status_code=422)
            except HTTPException as error:
                response = JSONResponse({'detail': error.detail if isinstance(error.detail, dict) else {'code': 'access_denied'}}, status_code=error.status_code)
            except Exception:
                # No raw exception/logging/capture: private amounts can be in DB errors.
                response = JSONResponse({'detail': {'code': 'internal_error'}}, status_code=500)
            response.headers.update({'Cache-Control': 'private, no-store', 'Pragma': 'no-cache'})
            return response
        return handler


router = APIRouter(prefix='/master-rewards', route_class=PrivateRewardRoute)


@router.get('/policy', response_model=PolicyView)
async def policy(actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    await service.authorize(session, actor)
    return await service.policy(session)


@router.put('/policy', response_model=PolicyView)
async def set_policy(payload: PolicyCommand, key: str=Header(alias='Idempotency-Key'),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.command(session, actor, 'policy', payload, key)


@router.get('/masters')
async def masters(actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    await service.authorize(session, actor)
    rows = (await session.scalars(select(Master).join(AdminUser, AdminUser.id == Master.admin_user_id)
        .where(Master.is_active.is_(True), AdminUser.is_active.is_(True)).order_by(Master.id))).all()
    return {'items': [{'id': row.id, 'name': row.full_name} for row in rows]}


@router.get('/candidates')
async def candidates(order_id: int=Query(gt=0), actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    await service.authorize(session, actor)
    return await service.candidates(session, order_id)


@router.post('/entries', status_code=201, response_model=EntryView)
async def accrue(payload: EntryCommand, key: str=Header(alias='Idempotency-Key'),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.command(session, actor, 'entry', payload, key)


@router.post('/entries/{entry_id}/review', response_model=EntryView)
async def review(entry_id: int, payload: ReviewCommand, key: str=Header(alias='Idempotency-Key'),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.command(session, actor, 'review', payload, key, entry_id=entry_id)


@router.post('/settlements/preview', response_model=PreviewView)
async def preview(payload: SettlementPreview, actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    await service.authorize(session, actor)
    return (await service.preview_settlement(session, payload))[0]


@router.post('/settlements', status_code=201, response_model=SettlementView)
async def settle(payload: SettlementCommand, key: str=Header(alias='Idempotency-Key'),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.command(session, actor, 'settlement', payload, key)


@router.get('/operations/{key}')
async def operation(key: str, actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    actor, _ = await service.authorize(session, actor)
    try:
        key = str(UUID(key))
    except ValueError:
        service.fail('invalid_command', 422)
    row = await session.scalar(select(RewardOperation).where(RewardOperation.actor_id == actor.id, RewardOperation.key == key))
    if not row:
        service.fail('not_found', 404)
    return {'operation': row.operation, 'response': row.response}


# Register concrete read functions (rather than public/shared DTO extensions).
@router.get('/entries', response_model=PaginatedResponse[EntryView])
async def entries(master_id: int|None=Query(default=None,gt=0), date_from: date|None=None, date_to: date|None=None,
    page: int=Query(default=1,ge=1), page_size: int=Query(default=30,ge=1,le=100),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.listing(session, actor, master_id=master_id, date_from=date_from, date_to=date_to, page=page, page_size=page_size)


@router.get('/me/entries', response_model=PaginatedResponse[EntryView])
async def my_entries(date_from: date|None=None, date_to: date|None=None,
    page: int=Query(default=1,ge=1), page_size: int=Query(default=30,ge=1,le=100),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.listing(session, actor, own=True, date_from=date_from, date_to=date_to, page=page, page_size=page_size)


@router.get('/summary')
async def summary(master_id: int|None=Query(default=None,gt=0), date_from: date|None=None, date_to: date|None=None,
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.summary(session, actor, master_id=master_id, date_from=date_from, date_to=date_to)


@router.get('/me/summary')
async def my_summary(date_from: date|None=None, date_to: date|None=None,
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.summary(session, actor, own=True, date_from=date_from, date_to=date_to)


@router.get('/settlements', response_model=PaginatedResponse[SettlementView])
async def settlements(master_id: int|None=Query(default=None,gt=0), date_from: date|None=None, date_to: date|None=None,
    page: int=Query(default=1,ge=1), page_size: int=Query(default=30,ge=1,le=100),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.listing(session, actor, settlements=True, master_id=master_id, date_from=date_from, date_to=date_to, page=page, page_size=page_size)


@router.get('/me/settlements', response_model=PaginatedResponse[SettlementView])
async def my_settlements(date_from: date|None=None, date_to: date|None=None,
    page: int=Query(default=1,ge=1), page_size: int=Query(default=30,ge=1,le=100),
    actor=Depends(get_current_admin_user), session: AsyncSession=Depends(get_db_session)):
    return await service.listing(session, actor, own=True, settlements=True, date_from=date_from, date_to=date_to, page=page, page_size=page_size)
