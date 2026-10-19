from datetime import datetime
from typing import Literal
from pydantic import BaseModel,Field

Reason=Literal['pending_order','payment_without_sale','fiscal_unknown','fiscal_stale','financial_exception','payment_resolution_pending','tracking_stale','np_return_pending','sms_uncertain','sms_queue_stale']
class WatchIssue(BaseModel):
    reason:Reason
    entity_id:str
    order_id:int|None
    since:datetime
    authority:Literal['test','unverified']
    reserved_units:int|None
class BackgroundProcess(BaseModel):
    role:Literal['disabled','local','owner','follower','unknown']
    state:Literal['disabled','starting','running','standby','lost','stopping','stopped','failed','unknown']
    last_error_code:str|None
    scheduler_names:list[str]=Field(default_factory=list)
class ShopOperationsWatch(BaseModel):
    observed_at:datetime
    stale_minutes:int
    counts:dict[Reason,int]
    total:int
    truncated:bool
    issues:list[WatchIssue]
    background_process:BackgroundProcess
    automatic_actions:bool
