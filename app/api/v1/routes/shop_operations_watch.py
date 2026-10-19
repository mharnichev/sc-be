from fastapi import APIRouter,Depends,Query,Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.shop_operations_watch import ShopOperationsWatch
from app.services.shop_operations_watch import snapshot
backoffice_router=APIRouter()

@backoffice_router.get('/shop-operations-watch',response_model=ShopOperationsWatch)
async def watch(request:Request,limit:int=Query(default=50,ge=1,le=100),stale_minutes:int=Query(default=60,ge=1,le=10080),
    actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await snapshot(session,actor,limit=limit,stale_minutes=stale_minutes,
                          process_state=getattr(request.app.state,'background_workers',None))
