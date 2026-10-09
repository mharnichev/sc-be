"""Taxonomy parent invariants against isolated local PostgreSQL."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.models.category import Category
from app.services.category import CategoryService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401

pytestmark = pytest.mark.anyio


async def seed_categories(database):
    async with database() as session:
        categories = [Category(name=name, slug=name.lower()) for name in ["Alpha", "Beta", "Gamma"]]
        session.add_all(categories)
        await session.commit()
        return [category.id for category in categories]


async def test_opposite_parent_updates_cannot_commit_a_cycle(database, monkeypatch):
    alpha, beta, _ = await seed_categories(database)
    ready = asyncio.Barrier(2)
    checked = asyncio.Barrier(2)
    first_checked = asyncio.Event()
    release_first = asyncio.Event()
    mode_known = asyncio.Event()
    state = {"locked": False, "pids": {}, "blocked": False}

    async def attempt(label, own_id, parent_id):
        service = CategoryService()
        async with database() as session:
            # Both ancestor identity maps intentionally predate the first commit.
            cached = list((await session.scalars(select(Category))).all())
            category = next(item for item in cached if item.id == own_id)
            state["pids"][label] = await session.scalar(text("SELECT pg_backend_pid()"))
            execute = session.execute
            ensure = service._ensure_no_cycle

            async def intercept(statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement):
                    state["locked"] = True
                    mode_known.set()
                    if label == "second":
                        await asyncio.wait_for(first_checked.wait(), 8)
                return await execute(statement, *args, **kwargs)

            async def cycle_check(*args, **kwargs):
                await ensure(*args, **kwargs)
                mode_known.set()
                if state["locked"]:
                    if label == "first":
                        first_checked.set()
                        await asyncio.wait_for(release_first.wait(), 8)
                else:
                    await asyncio.wait_for(checked.wait(), 8)

            monkeypatch.setattr(session, "execute", intercept)
            monkeypatch.setattr(service, "_ensure_no_cycle", cycle_check)
            await asyncio.wait_for(ready.wait(), 8)
            try:
                await service.update_category(session, category, {"parent_id": parent_id})
                return 200
            except HTTPException as error:
                await session.rollback()
                assert await session.scalar(text("SELECT 1")) == 1
                return error.status_code

    async def observer():
        await asyncio.wait_for(mode_known.wait(), 8)
        if not state["locked"]:
            return
        await asyncio.wait_for(first_checked.wait(), 8)
        async with database() as session:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                if await session.scalar(text("SELECT :first = ANY(pg_blocking_pids(:second))"),
                                        {"first": state["pids"]["first"], "second": state["pids"]["second"]}):
                    state["blocked"] = True
                    release_first.set()
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("Taxonomy writer did not wait for the first transaction")

    tasks = [asyncio.create_task(attempt("first", alpha, beta)),
             asyncio.create_task(attempt("second", beta, alpha)), asyncio.create_task(observer())]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks), 12)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(set(state["pids"].values())) == 2
    assert sorted(results[:2]) == [200, 400]
    if state["locked"]:
        assert state["blocked"]
    async with database() as session:
        parents = dict((await session.execute(select(Category.id, Category.parent_id))).all())
        assert not (parents[alpha] == beta and parents[beta] == alpha)


async def test_existing_corrupt_ancestor_cycle_is_rejected_in_bounded_time(database):
    alpha, beta, gamma = await seed_categories(database)
    async with database() as session:
        a, b = await session.get(Category, alpha), await session.get(Category, beta)
        a.parent_id, b.parent_id = beta, alpha
        await session.commit()
    async with database() as session:
        category = await session.get(Category, gamma)
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(CategoryService().update_category(session, category, {"parent_id": alpha}), 2)
        assert error.value.status_code == 400
        await session.rollback()
        assert (await session.get(Category, gamma)).parent_id is None


@pytest.mark.parametrize("operation", ["self", "descendant", "missing", "clear", "valid", "ordinary"])
async def test_parent_update_compatibility(database, operation):
    alpha, beta, gamma = await seed_categories(database)
    async with database() as session:
        a, b = await session.get(Category, alpha), await session.get(Category, beta)
        b.parent_id = alpha
        await session.commit()
        payload = {"self": {"parent_id": alpha}, "descendant": {"parent_id": beta},
                   "missing": {"parent_id": 999999}, "clear": {"parent_id": None},
                   "valid": {"parent_id": gamma}, "ordinary": {"name": "Renamed", "is_active": False}}[operation]
        if operation in {"self", "descendant", "missing"}:
            with pytest.raises(HTTPException) as error:
                await CategoryService().update_category(session, a, payload)
            assert error.value.status_code == (404 if operation == "missing" else 400)
            await session.rollback()
            assert (await session.get(Category, alpha)).parent_id is None
        else:
            updated = await CategoryService().update_category(session, b if operation == "clear" else a, payload)
            for key, value in payload.items():
                assert getattr(updated, key) == value
