from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.product import Product

HiddenReason = Literal["product", "category", "parent_category"]


@dataclass(frozen=True)
class VisibilityState:
    """Effective catalog visibility for one entity.

    The state is deliberately immutable: it is safe to pass around while a
    request is assembling several response objects from the same context.
    """

    is_effectively_visible: bool
    hidden_reason: HiddenReason | None


class CatalogVisibility:
    """Batch visibility context for a category graph and its products.

    Category ancestry is resolved in memory from one preloaded category set.
    This keeps response construction free of per-item database lookups and
    also provides SQL predicates for collection endpoints.
    """

    def __init__(
        self,
        categories_by_id: dict[int, Category],
        category_states: dict[int, VisibilityState],
    ) -> None:
        self.categories_by_id = categories_by_id
        self.category_states = category_states
        children_by_parent: dict[int | None, list[int]] = {}
        for category in categories_by_id.values():
            children_by_parent.setdefault(category.parent_id, []).append(category.id)
        self._children_by_parent = children_by_parent

    @classmethod
    def from_categories(cls, categories: Iterable[Category]) -> CatalogVisibility:
        categories_by_id = {category.id: category for category in categories}
        states: dict[int, VisibilityState] = {}
        visiting: set[int] = set()

        def resolve(category_id: int) -> VisibilityState:
            cached = states.get(category_id)
            if cached is not None:
                return cached

            category = categories_by_id[category_id]
            if not category.is_active:
                state = VisibilityState(False, "category")
                states[category_id] = state
                return state

            # CategoryService prevents cycles, but this guard keeps a corrupt
            # pre-existing graph from recursing forever.
            if category_id in visiting:
                return VisibilityState(False, "parent_category")

            visiting.add(category_id)
            if category.parent_id is None or category.parent_id not in categories_by_id:
                state = VisibilityState(True, None)
            else:
                parent_state = resolve(category.parent_id)
                state = (
                    VisibilityState(True, None)
                    if parent_state.is_effectively_visible
                    else VisibilityState(False, "parent_category")
                )
            visiting.remove(category_id)
            states[category_id] = state
            return state

        for category_id in categories_by_id:
            resolve(category_id)
        return cls(categories_by_id, states)

    @classmethod
    async def load(cls, session: AsyncSession) -> CatalogVisibility:
        result = await session.execute(select(Category))
        return cls.from_categories(result.scalars().all())

    def category_state(self, category_id: int) -> VisibilityState:
        return self.category_states[category_id]

    def product_state(self, product: Product) -> VisibilityState:
        if not product.is_active:
            return VisibilityState(False, "product")
        if product.category_id is None:
            return VisibilityState(True, None)
        category = self.categories_by_id.get(product.category_id)
        if category is None:
            # A valid database FK makes this impossible; treating an orphan as
            # uncategorized is safer than accidentally hiding it forever.
            return VisibilityState(True, None)
        category_state = self.category_state(category.id)
        if category_state.is_effectively_visible:
            return VisibilityState(True, None)
        return VisibilityState(False, category_state.hidden_reason or "category")

    def product_states(self, products: Iterable[Product]) -> dict[int, VisibilityState]:
        return {product.id: self.product_state(product) for product in products}

    def visible_category_ids(self) -> set[int]:
        return {
            category_id
            for category_id, state in self.category_states.items()
            if state.is_effectively_visible
        }

    def category_parents(self) -> dict[int, int | None]:
        """Return the already-loaded category ancestry used by promotions."""
        return {
            category_id: category.parent_id
            for category_id, category in self.categories_by_id.items()
        }

    def visible_product_clause(self, product_model: type[Product] = Product):
        category_clause = product_model.category_id.is_(None)
        visible_ids = self.visible_category_ids()
        if visible_ids:
            category_clause = or_(category_clause, product_model.category_id.in_(visible_ids))
        return product_model.is_active.is_(True) & category_clause

    def visible_category_clause(self, category_model: type[Category] = Category):
        visible_ids = self.visible_category_ids()
        if not visible_ids:
            return false()
        return category_model.id.in_(visible_ids)

    def descendant_ids(self, category_id: int) -> set[int]:
        if category_id not in self.categories_by_id:
            return set()
        descendants: set[int] = set()
        stack = [category_id]
        while stack:
            current_id = stack.pop()
            if current_id in descendants:
                continue
            descendants.add(current_id)
            stack.extend(self._children_by_parent.get(current_id, ()))
        return descendants

    def is_available_for_purchase(self, product: Product) -> bool:
        return (
            self.product_state(product).is_effectively_visible
            and product.stock_quantity > 0
            and product.availability_status != "out_of_stock"
        )
