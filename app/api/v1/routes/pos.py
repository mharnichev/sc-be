from datetime import date
from fastapi import APIRouter,Depends,Header,HTTPException,Query,Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse
from app.core.database import get_db_session
from app.core import error_reporting
from app.dependencies.auth import get_current_admin_user
from app.schemas.common import PaginatedResponse
from app.schemas.pos import CreateCheckout,AdoptCheckout,RevisionCommand,CheckoutView,ProductOffer,SaleSummary,SaleDetail,Capabilities,OrderStatus
from app.services import pos

class PrivatePOSRoute(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def handler(request):
            try:response=await original(request)
            except RequestValidationError:response=JSONResponse({'detail':{'code':'invalid_command'}},status_code=422)
            except HTTPException as error:
                detail=error.detail if isinstance(error.detail,dict) else {'code':'access_denied' if error.status_code in {401,403} else 'pos_error'}
                response=JSONResponse({'detail':{key:value for key,value in detail.items() if key in {'code','current_offer'}}},status_code=error.status_code)
                if error.status_code==401:response.headers['WWW-Authenticate']='Bearer'
            except Exception:
                error_reporting.report('request','unexpected')
                response=JSONResponse({'detail':{'code':'pos_internal_error'}},status_code=500)
            response.headers.update({'Cache-Control':'private, no-store','Pragma':'no-cache'})
            return response
        return handler

router=APIRouter(prefix='/pos',route_class=PrivatePOSRoute)

@router.get('/capabilities',response_model=Capabilities)
async def capabilities(actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.capabilities(session,actor)

@router.get('/products',response_model=PaginatedResponse[ProductOffer])
async def products(q:str=Query(default='',max_length=120),page:int=Query(default=1,ge=1),page_size:int=Query(default=20,ge=1,le=50),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.products(session,actor,q=q,page=page,page_size=page_size)

@router.post('/checkouts',response_model=CheckoutView,status_code=201)
async def create(payload:CreateCheckout,response:Response,key:str=Header(alias='Idempotency-Key'),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    result=await pos.create(session,actor,payload,key)
    response.status_code=201 if result.pop("_created") else 200
    return result

@router.post('/checkouts/adopt',response_model=CheckoutView)
async def adopt(payload:AdoptCheckout,key:str=Header(alias='Idempotency-Key'),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.adopt(session,actor,payload,key)

@router.get('/checkouts/active',response_model=PaginatedResponse[CheckoutView])
async def active(page:int=Query(default=1,ge=1),page_size:int=Query(default=20,ge=1,le=50),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.active(session,actor,page=page,page_size=page_size)

@router.get('/checkouts/by-intent/{intent_id}',response_model=CheckoutView)
async def by_intent(intent_id:str,actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.by_intent(session,actor,intent_id)

@router.get('/checkouts/{checkout_id}',response_model=CheckoutView)
async def state(checkout_id:str,actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.state(session,actor,checkout_id)

@router.post('/checkouts/{checkout_id}/resume',response_model=CheckoutView)
async def resume(checkout_id:str,payload:RevisionCommand,key:str=Header(alias='Idempotency-Key'),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.resume(session,actor,checkout_id,payload,key)

@router.post('/checkouts/{checkout_id}/refresh',response_model=CheckoutView)
async def refresh(checkout_id:str,payload:RevisionCommand,key:str=Header(alias='Idempotency-Key'),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.refresh(session,actor,checkout_id,payload,key)

@router.post('/checkouts/{checkout_id}/handover',response_model=CheckoutView)
async def handover(checkout_id:str,payload:RevisionCommand,key:str=Header(alias='Idempotency-Key'),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.handover(session,actor,checkout_id,payload,key)

@router.get('/sales',response_model=PaginatedResponse[SaleSummary])
async def sales(q:str=Query(default='',max_length=120),employee_user_id:int|None=Query(default=None,gt=0,le=2147483647),order_status:OrderStatus|None=None,date_from:date|None=None,date_to:date|None=None,page:int=Query(default=1,ge=1),page_size:int=Query(default=20,ge=1,le=100),actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.sales(session,actor,q=q,employee_user_id=employee_user_id,order_status=order_status,date_from=date_from,date_to=date_to,page=page,page_size=page_size)

@router.get('/sales/{order_id}',response_model=SaleDetail)
async def detail(order_id:int,actor=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await pos.sale_detail(session,actor,order_id)
