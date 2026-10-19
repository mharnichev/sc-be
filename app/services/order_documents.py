"""Local immutable order snapshots. This module never contacts a provider."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any
import hashlib
import json
import re
from uuid import UUID

from fastapi import HTTPException
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core.security import create_scoped_token, decode_token
from app.models.order import Order, OrderIntent, PreliminaryDocument

NOTICE = "Попереднє замовлення. Не є фіскальним чеком"


def digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def operation_key(raw: str, scope: str) -> str:
    if not isinstance(raw, str) or len(raw) > 200:
        raise HTTPException(422, detail={"code": "invalid_idempotency_key", "message": "Use a random UUIDv4 or at least 32 random URL-safe characters"})
    try:
        valid = UUID(raw).version == 4
    except ValueError:
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{32,200}", raw))
    if not valid:
        raise HTTPException(422, detail={"code": "invalid_idempotency_key", "message": "Use a random UUIDv4 or at least 32 random URL-safe characters"})
    return digest([scope, raw])


async def claim_intent(session: AsyncSession, key: str, payload_hash: str) -> OrderIntent:
    # The unique row is inserted in the SAME transaction as stock, promotion
    # usage, order and document. Concurrent inserts wait for commit/rollback;
    # a crash leaves either all effects or none. No process-local lock.
    dialect = session.bind.dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    if insert is None:
        raise RuntimeError("Order idempotency requires PostgreSQL or SQLite")
    await session.execute(
        insert(OrderIntent).values(scope_key=key, payload_hash=payload_hash)
        .on_conflict_do_nothing(index_elements=["scope_key"])
    )
    intent = (await session.execute(
        select(OrderIntent).where(OrderIntent.scope_key == key)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    if intent.payload_hash != payload_hash:
        raise HTTPException(409, detail={"code": "idempotency_conflict", "message": "Key already used for another request"})
    return intent


def minor(value: Decimal) -> int:
    return int((Decimal(value) * 100).to_integral_exact())


def make_document(
    order: Order, *, version: int = 1, operation: str | None = None,
    request_hash: str | None = None,
) -> PreliminaryDocument:
    now = datetime.now(UTC)
    return PreliminaryDocument(
        order_id=order.id, version=version, number=f"SC-{order.id:08d}-{version}",
        operation_key=operation, request_hash=request_hash, created_at=now,
        guest_expires_at=now + timedelta(days=7) if order.customer_id is None else None,
        snapshot={
            "document_kind": "preliminary", "notice": NOTICE, "simulation": bool(order.simulation), "order_id": order.id,
            "order_revision": order.revision, "currency": "UAH",
            "items": [{"product_id": i.product_id, "name": i.product_name, "sku": i.product_sku,
                       "quantity": i.quantity, "base_price_minor": minor(i.base_price if i.base_price is not None else i.price),
                       "price_minor": minor(i.price), "discount_minor": minor(i.discount_amount),
                       "total_minor": minor(i.total_price if i.total_price is not None else i.price * i.quantity)} for i in order.items],
            "subtotal_minor": minor(order.subtotal_amount), "discount_minor": minor(order.discount_amount),
            "amount_minor": minor(order.total_amount),
        },
    )


def document_url(document: PreliminaryDocument, *, owner: bool = False, admin: bool = False) -> str:
    suffix = f"/{document.order_id}/preliminary-documents/{document.id}/print"
    if admin:
        return "/api/v1/backoffice/orders" + suffix
    if owner or document.guest_expires_at is None:
        return "/api/v1/public/orders/me" + suffix
    token = create_scoped_token(document.id, "preliminary_document", document.guest_expires_at)
    return "/api/v1/public/order-documents/" + token


def token_document_id(token: str) -> int:
    try:
        data = decode_token(token)
        if data.get("scope") != "preliminary_document":
            raise ValueError()
        return int(data["sub"])
    except (JWTError, ValueError, TypeError, KeyError):
        raise HTTPException(404, "Document not found") from None


def printable(document: PreliminaryDocument) -> str:
    snapshot = document.snapshot

    def money(amount: int) -> str:
        return f"{Decimal(amount) / 100:.2f}"

    rows = "".join(
        f"<tr><td>{escape(str(item['name'] or ''))}</td>"
        f"<td>{item['quantity']}</td><td>{money(item['price_minor'])}</td>"
        f"<td>{money(item['total_minor'])}</td></tr>"
        for item in snapshot["items"]
    )
    return (
        '<!doctype html><html lang="uk"><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        f'<title>{escape(document.number)}</title>'
        '<style>body{font:16px sans-serif;max-width:850px;margin:32px auto}'
        'td,th{padding:10px;text-align:left}</style>'
        f'<h1>{escape(document.number)}</h1><strong>{NOTICE}</strong>'
        + ('<p><strong>Тестове замовлення. Симуляція без реального розрахунку.</strong></p>' if snapshot.get('simulation') else '')
        +
        f'<p>Версія {document.version} · UAH</p>'
        '<table><thead><tr><th>Товар</th><th>Кількість</th>'
        '<th>Ціна</th><th>Сума</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<p>Знижка: {money(snapshot["discount_minor"])} UAH</p>'
        f'<h2>Разом: {money(snapshot["amount_minor"])} UAH</h2>'
        '<p>Для друку скористайтеся функцією друку браузера.</p></html>'
    )
