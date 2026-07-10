from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import logging
from math import ceil

from sqlalchemy import bindparam, delete, distinct, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.shop import ProductView

logger = logging.getLogger(__name__)

_POPULARITY_LOCK_ID = 7_130_459_201
_SCORE_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class PopularitySignals:
    unique_views: int = 0
    paid_orders: int = 0
    purchased_units: int = 0


@dataclass(frozen=True)
class PopularityResult:
    score: Decimal
    rank: int | None
    is_top: bool


@dataclass(frozen=True)
class ProductViewResult:
    recorded: bool
    viewed_on: date


def build_visitor_hash(
    *,
    secret: str,
    customer_id: int | None,
    visitor_id: str | None,
    client_host: str | None,
    user_agent: str | None,
) -> str:
    if customer_id is not None:
        identity = f"customer:{customer_id}"
    elif visitor_id and visitor_id.strip():
        identity = f"visitor:{visitor_id.strip()[:200]}"
    else:
        identity = f"anonymous:{client_host or '-'}:{(user_agent or '-')[:500]}"
    return sha256(f"{secret}:{identity}".encode("utf-8")).hexdigest()


def is_refresh_due(last_calculated_at: datetime | None, now: datetime, refresh_interval_days: int) -> bool:
    if last_calculated_at is None:
        return True
    if last_calculated_at.tzinfo is None:
        last_calculated_at = last_calculated_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return last_calculated_at <= now - timedelta(days=refresh_interval_days)


def _percentile_ranks(values: dict[int, int]) -> dict[int, Decimal]:
    if not values:
        return {}
    if max(values.values(), default=0) <= 0:
        return {product_id: Decimal("0") for product_id in values}
    if len(values) == 1:
        product_id = next(iter(values))
        return {product_id: Decimal("1")}

    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = Decimal(len(ordered) - 1)
    ranks: dict[int, Decimal] = {}
    start = 0
    while start < len(ordered):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[start][1]:
            end += 1
        average_position = Decimal(start + end) / Decimal("2")
        percentile = average_position / denominator
        for index in range(start, end + 1):
            ranks[ordered[index][0]] = percentile
        start = end + 1
    return ranks


def calculate_popularity_results(
    signals: dict[int, PopularitySignals],
    *,
    top_fraction: float,
    max_top_products: int,
    min_unique_views: int,
    min_paid_orders: int,
) -> dict[int, PopularityResult]:
    view_ranks = _percentile_ranks({product_id: item.unique_views for product_id, item in signals.items()})
    unit_ranks = _percentile_ranks({product_id: item.purchased_units for product_id, item in signals.items()})
    scores = {
        product_id: (
            Decimal("0.4") * view_ranks.get(product_id, Decimal("0"))
            + Decimal("0.6") * unit_ranks.get(product_id, Decimal("0"))
        ).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)
        for product_id in signals
    }
    candidates = [
        product_id
        for product_id, item in signals.items()
        if item.unique_views >= min_unique_views or item.paid_orders >= min_paid_orders
    ]
    candidates.sort(
        key=lambda product_id: (
            -scores[product_id],
            -signals[product_id].purchased_units,
            -signals[product_id].unique_views,
            product_id,
        )
    )
    top_count = min(max_top_products, ceil(len(candidates) * top_fraction)) if candidates else 0
    top_ids = set(candidates[:top_count])
    ranks = {product_id: index for index, product_id in enumerate(candidates, start=1)}
    return {
        product_id: PopularityResult(
            score=scores[product_id],
            rank=ranks.get(product_id),
            is_top=product_id in top_ids,
        )
        for product_id in signals
    }


class ProductPopularityService:
    async def record_view(
        self,
        session: AsyncSession,
        *,
        product_id: int,
        visitor_hash: str,
        now: datetime | None = None,
    ) -> ProductViewResult:
        current = now or datetime.now(UTC)
        viewed_on = current.date()
        if session.get_bind().dialect.name == "postgresql":
            stmt = (
                postgresql_insert(ProductView)
                .values(product_id=product_id, visitor_hash=visitor_hash, viewed_on=viewed_on)
                .on_conflict_do_nothing(constraint="uq_product_views_product_visitor_day")
                .returning(ProductView.id)
            )
            recorded = (await session.execute(stmt)).scalar_one_or_none() is not None
        else:
            existing = (
                await session.execute(
                    select(ProductView.id).where(
                        ProductView.product_id == product_id,
                        ProductView.visitor_hash == visitor_hash,
                        ProductView.viewed_on == viewed_on,
                    )
                )
            ).scalar_one_or_none()
            recorded = existing is None
            if recorded:
                session.add(
                    ProductView(
                        product_id=product_id,
                        visitor_hash=visitor_hash,
                        viewed_on=viewed_on,
                    )
                )
        await session.commit()
        return ProductViewResult(recorded=recorded, viewed_on=viewed_on)

    async def refresh_if_due(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> bool:
        current = now or datetime.now(UTC)
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            locked = (
                await session.execute(select(func.pg_try_advisory_xact_lock(_POPULARITY_LOCK_ID)))
            ).scalar_one()
            if not locked:
                await session.rollback()
                return False

        last_calculated_at = (await session.execute(select(func.max(Product.top_calculated_at)))).scalar_one_or_none()
        if not force and not is_refresh_due(
            last_calculated_at,
            current,
            settings.product_top_refresh_interval_days,
        ):
            await session.rollback()
            return False

        try:
            await self.recalculate(session, now=current)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return True

    async def recalculate(self, session: AsyncSession, *, now: datetime) -> None:
        window_start = now - timedelta(days=settings.product_top_window_days)
        products = list(
            (
                await session.execute(
                    select(Product.id, Product.is_active, Product.stock_quantity)
                )
            ).all()
        )

        view_rows = (
            await session.execute(
                select(ProductView.product_id, func.count(ProductView.id))
                .where(ProductView.viewed_on >= window_start.date(), ProductView.viewed_on <= now.date())
                .group_by(ProductView.product_id)
            )
        ).all()
        views = {product_id: int(count) for product_id, count in view_rows}

        order_rows = (
            await session.execute(
                select(
                    OrderItem.product_id,
                    func.count(distinct(OrderItem.order_id)),
                    func.coalesce(func.sum(OrderItem.quantity), 0),
                )
                .select_from(OrderItem)
                .join(Order, Order.id == OrderItem.order_id)
                .where(
                    Order.status.in_((OrderStatus.paid, OrderStatus.completed)),
                    Order.created_at >= window_start,
                    Order.created_at <= now,
                )
                .group_by(OrderItem.product_id)
            )
        ).all()
        orders = {
            product_id: (int(order_count), int(unit_count))
            for product_id, order_count, unit_count in order_rows
        }

        eligible_signals = {
            product.id: PopularitySignals(
                unique_views=views.get(product.id, 0),
                paid_orders=orders.get(product.id, (0, 0))[0],
                purchased_units=orders.get(product.id, (0, 0))[1],
            )
            for product in products
            if product.is_active and product.stock_quantity > 0
        }
        results = calculate_popularity_results(
            eligible_signals,
            top_fraction=settings.product_top_fraction,
            max_top_products=settings.product_top_max_products,
            min_unique_views=settings.product_top_min_unique_views,
            min_paid_orders=settings.product_top_min_paid_orders,
        )

        cache_rows: list[dict[str, object]] = []
        for product in products:
            paid_orders, purchased_units = orders.get(product.id, (0, 0))
            result = results.get(product.id)
            cache_rows.append(
                {
                    "_product_id": product.id,
                    "_is_top": result.is_top if result is not None else False,
                    "_top_score": result.score if result is not None else Decimal("0"),
                    "_top_rank": result.rank if result is not None else None,
                    "_top_unique_views": views.get(product.id, 0),
                    "_top_paid_orders": paid_orders,
                    "_top_purchased_units": purchased_units,
                    "_top_calculated_at": now,
                }
            )

        if cache_rows:
            product_table = Product.__table__
            cache_update = (
                product_table.update()
                .where(product_table.c.id == bindparam("_product_id"))
                .values(
                    is_top=bindparam("_is_top"),
                    top_score=bindparam("_top_score"),
                    top_rank=bindparam("_top_rank"),
                    top_unique_views_30d=bindparam("_top_unique_views"),
                    top_paid_orders_30d=bindparam("_top_paid_orders"),
                    top_purchased_units_30d=bindparam("_top_purchased_units"),
                    top_calculated_at=bindparam("_top_calculated_at"),
                    updated_at=product_table.c.updated_at,
                )
            )
            await session.execute(cache_update, cache_rows)

        retention_cutoff = (now - timedelta(days=settings.product_view_retention_days)).date()
        await session.execute(delete(ProductView).where(ProductView.viewed_on < retention_cutoff))


async def run_product_popularity_scheduler() -> None:
    interval_seconds = settings.product_top_check_interval_days * 24 * 60 * 60
    while True:
        try:
            async with AsyncSessionLocal() as session:
                refreshed = await product_popularity_service.refresh_if_due(session)
                if refreshed:
                    logger.info("Product TOP cache refreshed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Product TOP cache check failed")
        await asyncio.sleep(interval_seconds)


product_popularity_service = ProductPopularityService()
