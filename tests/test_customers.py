from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.api.v1.routes import customers as customer_routes
from app.dependencies.common import PaginationParams
from app.models.customer import Customer


class FakeScalarResult:
    def __init__(self, values: list[int]) -> None:
        self.values = values

    def all(self) -> list[int]:
        return self.values


class FakeExecuteResult:
    def __init__(self, values: list[int]) -> None:
        self.values = values

    def scalars(self) -> FakeScalarResult:
        return FakeScalarResult(self.values)


class FakeSession:
    def __init__(self, connected_customer_ids: list[int]) -> None:
        self.connected_customer_ids = connected_customer_ids
        self.statements: list[object] = []

    async def execute(self, statement):  # noqa: ANN001, ANN201
        self.statements.append(statement)
        return FakeExecuteResult(self.connected_customer_ids)


class FakeCustomerRepository:
    def __init__(self) -> None:
        self.last_stmt = None

    async def list(self, _session, *, stmt, page: int, page_size: int):  # noqa: ANN001, ANN201, ARG002
        self.last_stmt = stmt
        return [_customer(1), _customer(2)], 2


def _customer(customer_id: int) -> Customer:
    now = datetime.now(UTC)
    return Customer(
        id=customer_id,
        phone=f"38000000000{customer_id}",
        email=None,
        name=f"Client {customer_id}",
        surname=None,
        notes=None,
        imported_total_spent=Decimal("0.00"),
        imported_last_visit_at=None,
        imported_is_new_client=False,
        is_active=True,
        phone_verified_at=now if customer_id == 1 else None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.anyio
async def test_backoffice_customer_list_includes_telegram_connected_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_repo = FakeCustomerRepository()
    monkeypatch.setattr(customer_routes, "repo", fake_repo)
    session = FakeSession([1])

    response = await customer_routes.backoffice_list_customers(
        pagination=PaginationParams(page=1, page_size=20),
        is_active=None,
        is_verified=None,
        telegram_connected="true",
        search=None,
        sort_by="created_at",
        sort_order="desc",
        _=object(),
        session=session,
    )

    assert [item.telegram_connected for item in response.items] == [True, False]
    assert len(session.statements) == 1
    assert "EXISTS" in str(fake_repo.last_stmt)
    assert "telegram_chat_id" in str(fake_repo.last_stmt)
