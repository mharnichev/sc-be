"""Server-owned scope for the simple master POS workflow."""
from fastapi import HTTPException
from sqlalchemy import select

from app.models.booking import Master


async def restricted_master(session, actor):
    if actor.is_superuser:
        return False
    return await session.scalar(select(Master.id).where(Master.admin_user_id == actor.id)) is not None


async def deny_master(session, actor):
    if await restricted_master(session, actor):
        raise HTTPException(403, "Masters may only use their own QR sales workflow")


async def own_sale(session, actor, order):
    restricted = await restricted_master(session, actor)
    if restricted:
        if not (actor.is_active and actor.can_record_sales and actor.can_accept_qr_payments):
            raise HTTPException(403, "Sales and QR payment permissions required")
        if (order.created_by_user_id != actor.id or order.channel != "backoffice_pos"):
            raise HTTPException(404, "Order not found")
    return restricted
