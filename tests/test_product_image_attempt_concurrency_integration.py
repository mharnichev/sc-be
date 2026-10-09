"""Image-attempt ownership with real isolated PostgreSQL and temporary storage."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.models.product import Product
from app.models.shop import ProductImage, ProductImageVariant
from app.models.upload import Upload
from app.services.studio_product_composer import ComposerError, ComposerRender
from app.utils import process_product_images as processing
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_product_image_processing_identity import cli_options, image_bytes


pytestmark = pytest.mark.anyio
WORKER = ContextVar("synthetic_image_attempt_worker", default=None)
ACTIVE_SESSION = ContextVar("synthetic_image_attempt_session", default=None)


async def bounded(operation, *, timeout=10):
    return await asyncio.wait_for(operation, timeout=timeout)


class ImageAttemptPipeline:
    def __init__(self, database, monkeypatch, root, source):
        self.database, self.monkeypatch = database, monkeypatch
        self.root, self.source = root, source
        self.args = cli_options(output_format="png")
        self.connections, self.pids = {}, {}
        self.calls, self.saved = [], []
        self.first_render = asyncio.Event()
        self.release_first = asyncio.Event()
        self.pause_first = False
        self.fail_first = False
        self.cold_barrier = False
        self.missing = {1: [], 2: []}
        self.missing_events = {1: asyncio.Event(), 2: asyncio.Event()}
        self.bad_upload = False
        self.commit_fault = None
        self.commit_fault_injected = False
        self.parent_wait_edges = []
        self.parent_wait_observed = False
        self.check_render_locks = False
        self.simultaneous_renders = False
        self.renders_ready = asyncio.Event()
        self.observe_publication_wait = False
        self.session_counts = {}
        self.wait_storage_until = None
        self.storage_crossed_deadline = False
        self.duplicate_upload_name = None
        self.output_flush_succeeded = False
        self.output_commit_error = None
        self.rollback_fault = None
        self.rollback_fault_injected = False
        self.cancel_after_flush = False
        self.cancel_after_flush_injected = False
        self.real_save = processing.save_image_upload

    @asynccontextmanager
    async def sessions(self):
        label = WORKER.get()
        self.session_counts[label] = self.session_counts.get(label, 0) + 1
        phase = self.session_counts[label]
        async with self.database(bind=self.connections[label]) as session:
            actual_execute, actual_commit = session.execute, session.commit
            actual_flush, actual_rollback = session.flush, session.rollback
            identity_reads = 0

            async def execute(statement, *args, **kwargs):
                nonlocal identity_reads
                result = await actual_execute(statement, *args, **kwargs)
                selects_variant = any(description.get("entity") is ProductImageVariant
                                      for description in getattr(statement, "column_descriptions", ()))
                locks_product = (getattr(statement, "_for_update_arg", None) is not None and any(
                    description.get("entity") is Product
                    for description in getattr(statement, "column_descriptions", ())))
                observe_parent = self.cold_barrier or (self.observe_publication_wait and phase >= 2)
                if observe_parent and locks_product and not self.parent_wait_observed:
                    self.parent_wait_observed = True
                    # After both genuine initial misses, the first owner holds
                    # Product until the observer proves its rival waits on it.
                    other = next(worker for worker in self.pids if worker != label)
                    async with asyncio.timeout(5):
                        while True:
                            blockers = await bounded(self.observer.scalar(
                                text("SELECT pg_blocking_pids(:pid)"), {"pid": self.pids[other]}))
                            if self.pids[label] in blockers:
                                self.parent_wait_edges.append([self.pids[other], self.pids[label]])
                                break
                            await asyncio.sleep(0.01)
                if self.cold_barrier and selects_variant and identity_reads < 1:
                    identity_reads += 1
                    frozen = result.freeze()
                    if not frozen().scalars().all():
                        self.missing[identity_reads].append(label)
                        if len(self.missing[identity_reads]) == 2:
                            self.missing_events[identity_reads].set()
                        await bounded(self.missing_events[identity_reads].wait(), timeout=5)
                    return frozen()
                return result

            async def commit():
                inject = (self.commit_fault is not None and not self.commit_fault_injected
                          and session.info.get("saved_output", False))
                try:
                    await actual_commit()
                except DBAPIError as exc:
                    if session.info.get("saved_output", False):
                        self.output_commit_error = exc
                    raise
                if inject:
                    self.commit_fault_injected = True
                    # The commit is real. The caller receives an ambiguous
                    # acknowledgement failure rather than a simulated rollback.
                    raise self.commit_fault

            async def flush(*args, **kwargs):
                await actual_flush(*args, **kwargs)
                if session.info.get("saved_output", False):
                    self.output_flush_succeeded = True
                    if self.cancel_after_flush and not self.cancel_after_flush_injected:
                        self.cancel_after_flush_injected = True
                        asyncio.current_task().cancel("Synthetic publication cancellation after upload flush")
                        await asyncio.sleep(0)

            async def rollback():
                await actual_rollback()
                if (self.rollback_fault is not None and not self.rollback_fault_injected
                        and session.info.get("saved_output", False)):
                    self.rollback_fault_injected = True
                    raise self.rollback_fault

            self.monkeypatch.setattr(session, "execute", execute)
            self.monkeypatch.setattr(session, "commit", commit)
            self.monkeypatch.setattr(session, "flush", flush)
            self.monkeypatch.setattr(session, "rollback", rollback)
            token = ACTIVE_SESSION.set(session)
            try:
                yield session
            finally:
                ACTIVE_SESSION.reset(token)

    async def save(self, file, **kwargs):
        data = await self.real_save(file, **kwargs)
        self.saved.append(dict(data))
        ACTIVE_SESSION.get().info["saved_output"] = True
        if self.wait_storage_until is not None:
            async with asyncio.timeout(3):
                while (await bounded(self.observer.scalar(text("SELECT clock_timestamp()")))) < self.wait_storage_until:
                    await asyncio.sleep(0.01)
            self.storage_crossed_deadline = True
        if self.duplicate_upload_name is not None:
            data = {**data, "file_name": self.duplicate_upload_name}
        if self.bad_upload:
            # Upload.file_name is NOT NULL: force a real PostgreSQL rejection
            # after the real storage function has finished writing its file.
            data = {**data, "file_name": None}
        return data

    async def render(self, path, **options):
        label = WORKER.get()
        assert Path(path).is_file()
        if self.check_render_locks:
            await bounded(self.observer.execute(select(Product).where(Product.id == self.source.product_id)
                                               .with_for_update(nowait=True)))
            await bounded(self.observer.execute(select(ProductImageVariant).where(
                ProductImageVariant.product_id == self.source.product_id).with_for_update(nowait=True)))
            await bounded(self.observer.rollback())
        self.calls.append(label)
        if self.simultaneous_renders:
            if len(self.calls) == 2:
                self.renders_ready.set()
            await bounded(self.renders_ready.wait(), timeout=5)
        if label == "A" and self.pause_first:
            self.first_render.set()
            await bounded(self.release_first.wait())
            if self.fail_first:
                raise ComposerError("Synthetic retired attempt failure")
        color = (20, 40, 60) if label == "A" else (100, 120, 140)
        return ComposerRender(image=image_bytes(color=color), processor_version="synthetic-composer",
                              audit={"project_id": f"synthetic-{label}"})

    async def run(self, label, **option_changes):
        counts = Counter()
        token = WORKER.set(label)
        try:
            # A physical connection remains reserved for each entire attempt,
            # even across its separate committed database phases.
            async with self.database.kw["bind"].connect() as connection:
                self.connections[label] = connection
                await bounded(connection.execute(text("SET lock_timeout = '5s'")))
                await bounded(connection.execute(text("SET statement_timeout = '8s'")))
                self.pids[label] = await bounded(connection.scalar(text("SELECT pg_backend_pid()")))
                await bounded(connection.commit())
                try:
                    options = {"output_format": "png", **option_changes}
                    await processing.process_source(self.source, cli_options(**options), counts)
                    error = None
                except (DBAPIError, OSError) as exc:
                    error = exc
                await bounded(connection.rollback())
                assert await bounded(connection.scalar(text("SELECT 1"))) == 1
                return SimpleNamespace(counts=counts, error=error)
        finally:
            WORKER.reset(token)

    async def evidence(self, record_property):
        assert len(set(self.pids.values())) == len(self.pids)
        assert self.observer_pid not in self.pids.values()
        assert await bounded(self.observer.scalar(text("SELECT 1"))) == 1
        record_property("worker_backend_pids", json.dumps(self.pids, sort_keys=True))
        record_property("observer_backend_pid", self.observer_pid)
        record_property("missing_read_workers", json.dumps(self.missing, sort_keys=True))
        record_property("parent_wait_edges", json.dumps(self.parent_wait_edges))

    async def seed_variant(self, *, status="pending", previous=False):
        async with self.database() as session:
            upload = None
            if previous:
                path = self.root / "previous.png"
                path.write_bytes(image_bytes())
                upload = Upload(file_name=path.name, file_path=str(path), file_url="/media/previous.png",
                                content_type="image/png", size=path.stat().st_size)
                session.add(upload)
                await bounded(session.flush())
            row = ProductImageVariant(
                product_id=self.source.product_id, source_image_id=self.source.source_image_id,
                source_key=self.source.source_key, source_url=self.source.source_url,
                source_fingerprint=hashlib.sha256((self.root / "original.png").read_bytes()).hexdigest(),
                preset=processing.PRESET,
                recipe_version="synthetic-previous-recipe" if previous else processing._recipe_identity(self.args),
                status=status, attempt_count=0, is_preferred=previous,
                output_upload_id=upload.id if upload else None, output_url=upload.file_url if upload else None,
            )
            session.add(row)
            await bounded(session.commit())
            return row.id


@pytest.fixture
async def image_pipeline(database, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_url_prefix", "/media")
    (tmp_path / "original.png").write_bytes(image_bytes())
    async with database() as session:
        product = Product(name="Synthetic attempt product", slug=f"attempt-{uuid4().hex}", price=Decimal("10"))
        session.add(product)
        await bounded(session.flush())
        image = ProductImage(product_id=product.id, image_url="/media/original.png", is_active=True)
        session.add(image)
        await bounded(session.commit())
        source = processing.SourceImage(product.id, image.id, f"gallery:{image.id}", image.image_url)
    pipeline = ImageAttemptPipeline(database, monkeypatch, tmp_path, source)

    class Composer:
        def __init__(self, *args, **kwargs):
            pass

        async def render_studio_light(self, path, **options):
            return await pipeline.render(path, **options)

    monkeypatch.setattr(processing, "AsyncSessionLocal", pipeline.sessions)
    monkeypatch.setattr(processing, "StudioProductComposerClient", Composer)
    monkeypatch.setattr(processing, "save_image_upload", pipeline.save)
    # Reserve the observer before workers start: a post-race checkout could
    # reuse a worker PID and would not establish a third independent backend.
    async with database.kw["bind"].connect() as observer_connection:
        pipeline.observer_pid = await bounded(observer_connection.scalar(text("SELECT pg_backend_pid()")))
        await bounded(observer_connection.commit())
        async with database(bind=observer_connection) as observer:
            pipeline.observer = observer
            yield pipeline


async def variant_snapshot(database, variant_id):
    async with database() as session:
        return tuple((await bounded(session.execute(
            select(ProductImageVariant.__table__).where(ProductImageVariant.id == variant_id),
        ))).one())


async def assert_one_success(pipeline):
    async with pipeline.database() as session:
        rows = list(await bounded(session.scalars(select(ProductImageVariant))))
        uploads = list(await bounded(session.scalars(select(Upload))))
        assert len(rows) == len(uploads) == 1
        row, upload = rows[0], uploads[0]
        assert row.status == "succeeded" and row.is_preferred
        assert row.attempt_token is None and row.lease_expires_at is None
        assert row.output_upload_id == upload.id and row.output_url == upload.file_url
        assert Path(upload.file_path).is_file()
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
        return row.id


async def test_cold_same_identity_claim_race_has_one_owner_and_no_unique_error(image_pipeline, record_property):
    pipeline = image_pipeline
    pipeline.cold_barrier = True
    outcomes = await bounded(asyncio.gather(pipeline.run("A"), pipeline.run("B")))
    await pipeline.evidence(record_property)
    assert set(pipeline.missing[1]) == {"A", "B"}
    errors = [getattr(outcome.error.orig, "sqlstate", None)
              for outcome in outcomes if isinstance(outcome.error, DBAPIError)]
    constraints = [getattr(getattr(outcome.error.orig, "__cause__", None), "constraint_name", None)
                   for outcome in outcomes if isinstance(outcome.error, DBAPIError)]
    record_property("database_error_sqlstates", json.dumps(errors))
    record_property("database_error_constraints", json.dumps(constraints))
    if errors:
        assert errors == ["23505"] and constraints == ["uq_product_image_variants_source_recipe"]
    assert all(outcome.error is None for outcome in outcomes), errors
    assert len(pipeline.calls) == 1
    assert sum(outcome.counts["processed"] for outcome in outcomes) == 1
    assert sum(outcome.counts["skipped"] for outcome in outcomes) == 1
    row_id = await assert_one_success(pipeline)
    async with pipeline.database() as session:
        assert (await bounded(session.get(ProductImageVariant, row_id))).attempt_count == 1


async def test_seeded_pending_claim_does_not_restart_a_live_composer_attempt(image_pipeline, record_property):
    pipeline = image_pipeline
    await pipeline.seed_variant()
    pipeline.pause_first = True
    first = asyncio.create_task(pipeline.run("A"))
    try:
        await bounded(pipeline.first_render.wait(), timeout=5)
        second = await bounded(pipeline.run("B"))
    finally:
        pipeline.release_first.set()
        result = await bounded(first)
    await pipeline.evidence(record_property)
    assert result.error is second.error is None
    assert result.counts == {"processed": 1} and second.counts == {"skipped": 1}
    assert pipeline.calls == ["A"]
    row_id = await assert_one_success(pipeline)
    async with pipeline.database() as session:
        assert (await bounded(session.get(ProductImageVariant, row_id))).attempt_count == 1


async def test_retired_attempt_failure_cannot_overwrite_reclaimed_success(image_pipeline, record_property):
    pipeline = image_pipeline
    row_id = await pipeline.seed_variant()
    pipeline.pause_first = pipeline.fail_first = True
    first = asyncio.create_task(pipeline.run("A"))
    try:
        await bounded(pipeline.first_render.wait(), timeout=5)
        # This stage runs against the current no-lease model. Once the additive
        # lease migration exists, expire the durable claim rather than waiting.
        if hasattr(ProductImageVariant, "lease_expires_at"):
            async with pipeline.database() as session:
                await bounded(session.execute(update(ProductImageVariant).where(ProductImageVariant.id == row_id)
                                              .values(lease_expires_at=text("clock_timestamp() - interval '1 second'"))))
                await bounded(session.commit())
        second = await bounded(pipeline.run("B"))
        assert second.error is None and second.counts == {"processed": 1}
        winner = await variant_snapshot(pipeline.database, row_id)
    finally:
        pipeline.release_first.set()
        retired = await bounded(first)
    await pipeline.evidence(record_property)
    assert retired.error is None
    assert await variant_snapshot(pipeline.database, row_id) == winner
    assert pipeline.calls == ["A", "B"]
    assert await assert_one_success(pipeline) == row_id


async def test_definite_output_flush_rejection_removes_only_its_new_file(image_pipeline):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    previous = await variant_snapshot(pipeline.database, previous_id)
    original = (pipeline.root / "original.png").read_bytes()
    pipeline.bad_upload = True
    result = await bounded(pipeline.run("A"))
    assert isinstance(result.error, DBAPIError) and result.error.orig.sqlstate == "23502"
    assert result.counts["processed"] == 0
    assert len(pipeline.saved) == 1
    assert not Path(pipeline.saved[0]["file_path"]).exists()
    assert await variant_snapshot(pipeline.database, previous_id) == previous
    assert (pipeline.root / "original.png").read_bytes() == original
    assert (pipeline.root / "previous.png").is_file()
    async with pipeline.database() as session:
        assert len(list(await bounded(session.scalars(select(Upload))))) == 1
        assert await bounded(session.scalar(text("SELECT 1"))) == 1


async def test_output_commit_acknowledgement_failure_preserves_durable_success_and_file(image_pipeline):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    pipeline.commit_fault = OSError("Synthetic output commit acknowledgement loss")
    original = (pipeline.root / "original.png").read_bytes()
    result = await bounded(pipeline.run("A"))
    assert pipeline.commit_fault_injected
    assert result.error is None or result.error is pipeline.commit_fault
    assert len(pipeline.saved) == 1 and Path(pipeline.saved[0]["file_path"]).is_file()
    async with pipeline.database() as session:
        rows = list(await bounded(session.scalars(select(ProductImageVariant).where(ProductImageVariant.id != previous_id))))
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "succeeded" and row.is_preferred and row.error_detail is None
        upload = await bounded(session.get(Upload, row.output_upload_id))
        assert upload.file_path == pipeline.saved[0]["file_path"] and row.output_url == upload.file_url
        assert not (await bounded(session.get(ProductImageVariant, previous_id))).is_preferred
        assert len(list(await bounded(session.scalars(select(Upload))))) == 2
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert (pipeline.root / "original.png").read_bytes() == original
    assert (pipeline.root / "previous.png").is_file()


async def expire_attempt(database, row_id):
    async with database() as session:
        await bounded(session.execute(update(ProductImageVariant).where(ProductImageVariant.id == row_id)
                                      .values(lease_expires_at=text("clock_timestamp() - interval '1 second'"))))
        await bounded(session.commit())


async def test_expired_owner_success_cannot_replace_reclaimed_winner_or_create_orphan(image_pipeline, record_property):
    pipeline = image_pipeline
    row_id = await pipeline.seed_variant()
    pipeline.pause_first = True
    first = asyncio.create_task(pipeline.run("A"))
    try:
        await bounded(pipeline.first_render.wait(), timeout=5)
        await expire_attempt(pipeline.database, row_id)
        second = await bounded(pipeline.run("B"))
        assert second.error is None and second.counts == {"processed": 1}
        winner = await variant_snapshot(pipeline.database, row_id)
    finally:
        pipeline.release_first.set()
        retired = await bounded(first)
    await pipeline.evidence(record_property)
    assert retired.error is None and retired.counts == {"skipped": 1}
    assert await variant_snapshot(pipeline.database, row_id) == winner
    assert len(pipeline.saved) == 1
    assert await assert_one_success(pipeline) == row_id
    async with pipeline.database() as session:
        assert (await bounded(session.get(ProductImageVariant, row_id))).attempt_count == 2


async def test_crashed_claim_stays_exclusive_until_expiry_then_reclaims_once(image_pipeline):
    pipeline = image_pipeline
    row_id = await pipeline.seed_variant()
    pipeline.pause_first = True
    first = asyncio.create_task(pipeline.run("A"))
    try:
        await bounded(pipeline.first_render.wait(), timeout=5)
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bounded(first)
    async with pipeline.database() as session:
        row = await bounded(session.get(ProductImageVariant, row_id))
        assert row.status == "processing" and row.attempt_token and row.lease_expires_at
        assert row.attempt_count == 1
        snapshot = await variant_snapshot(pipeline.database, row_id)
    second = await bounded(pipeline.run("B"))
    assert second.error is None and second.counts == {"skipped": 1}
    assert await variant_snapshot(pipeline.database, row_id) == snapshot
    await expire_attempt(pipeline.database, row_id)
    reclaimed = await bounded(pipeline.run("C"))
    assert reclaimed.error is None and reclaimed.counts == {"processed": 1}
    assert await assert_one_success(pipeline) == row_id
    async with pipeline.database() as session:
        assert (await bounded(session.get(ProductImageVariant, row_id))).attempt_count == 2
    assert pipeline.calls == ["A", "C"]


async def test_legacy_processing_without_lease_is_recoverable(image_pipeline):
    pipeline = image_pipeline
    row_id = await pipeline.seed_variant(status="processing")
    result = await bounded(pipeline.run("A"))
    assert result.error is None and result.counts == {"processed": 1}
    assert await assert_one_success(pipeline) == row_id
    async with pipeline.database() as session:
        assert (await bounded(session.get(ProductImageVariant, row_id))).attempt_count == 1


def immutable_rendition(row):
    return (row.source_fingerprint, row.recipe_version, row.output_upload_id, row.output_url,
            row.processor_version, row.processing_config, row.status, row.attempt_count)


async def test_different_recipes_serialize_publication_and_keep_last_completion_preferred(image_pipeline, record_property):
    pipeline = image_pipeline
    pipeline.pause_first = pipeline.check_render_locks = True
    first = asyncio.create_task(pipeline.run("A"))
    try:
        await bounded(pipeline.first_render.wait(), timeout=5)
        second = await bounded(pipeline.run("B", output_quality=95))
        assert second.error is None and second.counts == {"processed": 1}
        async with pipeline.database() as session:
            earlier = (await bounded(session.scalars(select(ProductImageVariant).where(
                ProductImageVariant.status == "succeeded")))).one()
            earlier_id, earlier_snapshot = earlier.id, immutable_rendition(earlier)
            earlier_upload = await bounded(session.get(Upload, earlier.output_upload_id))
            earlier_path, earlier_bytes = Path(earlier_upload.file_path), Path(earlier_upload.file_path).read_bytes()
    finally:
        pipeline.release_first.set()
        last = await bounded(first)
    await pipeline.evidence(record_property)
    assert last.error is None and last.counts == {"processed": 1}
    async with pipeline.database() as session:
        rows = list(await bounded(session.scalars(select(ProductImageVariant))))
        assert len(rows) == 2 and all(row.status == "succeeded" for row in rows)
        earlier = next(row for row in rows if row.id == earlier_id)
        later = next(row for row in rows if row.id != earlier_id)
        assert immutable_rendition(earlier) == earlier_snapshot and not earlier.is_preferred
        assert later.is_preferred and later.processing_config["project_id"] == "synthetic-A"
        assert all(row.attempt_token is None and row.lease_expires_at is None for row in rows)
        assert len(list(await bounded(session.scalars(select(Upload))))) == 2
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert earlier_path.read_bytes() == earlier_bytes
    assert len(pipeline.saved) == 2


async def test_cleanup_failure_preserves_primary_database_rejection_and_previous_files(image_pipeline, monkeypatch, caplog):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    previous = await variant_snapshot(pipeline.database, previous_id)
    pipeline.bad_upload = True
    cleanup_paths = []

    def fail_cleanup(path):
        cleanup_paths.append(path)
        raise OSError("Synthetic cleanup permission failure")

    monkeypatch.setattr(processing, "delete_upload_file", fail_cleanup)
    result = await bounded(pipeline.run("A"))
    assert isinstance(result.error, DBAPIError) and result.error.orig.sqlstate == "23502"
    assert cleanup_paths == [pipeline.saved[0]["file_path"]]
    assert Path(cleanup_paths[0]).is_file()
    assert "Failed to remove an uncommitted image-attempt output" in caplog.text
    assert await variant_snapshot(pipeline.database, previous_id) == previous
    assert (pipeline.root / "original.png").is_file() and (pipeline.root / "previous.png").is_file()
    async with pipeline.database() as session:
        assert len(list(await bounded(session.scalars(select(Upload))))) == 1


@pytest.mark.parametrize("retry", [False, True])
async def test_failed_recipe_requires_explicit_retry_for_a_new_lease(image_pipeline, retry):
    pipeline = image_pipeline
    row_id = await pipeline.seed_variant(status="failed")
    before = await variant_snapshot(pipeline.database, row_id)
    result = await bounded(pipeline.run("A", retry_failed=retry))
    assert result.error is None
    if retry:
        assert result.counts == {"processed": 1}
        assert await assert_one_success(pipeline) == row_id
    else:
        assert result.counts == {"skipped": 1} and pipeline.calls == []
        assert await variant_snapshot(pipeline.database, row_id) == before


async def test_dry_run_has_no_claim_render_or_output(image_pipeline):
    pipeline = image_pipeline
    result = await bounded(pipeline.run("A", dry_run=True))
    assert result.error is None and result.counts == {"queued": 1}
    assert pipeline.calls == pipeline.saved == []
    async with pipeline.database() as session:
        assert list(await bounded(session.scalars(select(ProductImageVariant)))) == []
        assert list(await bounded(session.scalars(select(Upload)))) == []


async def test_valid_success_replay_is_immutable_and_does_not_claim_again(image_pipeline):
    pipeline = image_pipeline
    first = await bounded(pipeline.run("A"))
    assert first.error is None and first.counts == {"processed": 1}
    row_id = await assert_one_success(pipeline)
    before = await variant_snapshot(pipeline.database, row_id)
    replay = await bounded(pipeline.run("B"))
    assert replay.error is None and replay.counts == {"skipped": 1}
    assert await variant_snapshot(pipeline.database, row_id) == before
    assert pipeline.calls == ["A"] and len(pipeline.saved) == 1


@pytest.mark.parametrize("options", [
    {"max_wait_seconds": float("nan")}, {"request_timeout": float("inf")},
    {"poll_seconds": 0}, {"request_timeout": -1}, {"max_wait_seconds": 1e308},
])
async def test_invalid_or_overflowing_lease_budget_cannot_persist_a_claim(image_pipeline, options):
    pipeline = image_pipeline
    result = await bounded(pipeline.run("A", **options))
    assert result.error is None and result.counts == {"failed": 1}
    assert pipeline.calls == pipeline.saved == []
    async with pipeline.database() as session:
        assert list(await bounded(session.scalars(select(ProductImageVariant)))) == []


async def test_simultaneous_different_recipe_publications_wait_on_parent_without_deadlock(image_pipeline, record_property):
    pipeline = image_pipeline
    pipeline.simultaneous_renders = pipeline.observe_publication_wait = True
    outcomes = await bounded(asyncio.gather(pipeline.run("A"), pipeline.run("B", output_quality=95)))
    await pipeline.evidence(record_property)
    assert len(pipeline.parent_wait_edges) == 1
    assert all(outcome.error is None and outcome.counts == {"processed": 1} for outcome in outcomes)
    async with pipeline.database() as session:
        rows = list(await bounded(session.scalars(select(ProductImageVariant))))
        assert len(rows) == 2 and all(row.status == "succeeded" for row in rows)
        assert sum(row.is_preferred for row in rows) == 1
        assert len({row.recipe_version for row in rows}) == 2
        assert all(row.attempt_count == 1 and row.attempt_token is None and row.lease_expires_at is None for row in rows)
        uploads = list(await bounded(session.scalars(select(Upload))))
        assert len(uploads) == 2 and all(Path(upload.file_path).is_file() for upload in uploads)
        assert await bounded(session.scalar(text("SELECT 1"))) == 1


async def test_real_storage_crossing_lease_expiry_rolls_back_and_reclaims_without_demoting_previous(image_pipeline):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    previous = await variant_snapshot(pipeline.database, previous_id)
    original = (pipeline.root / "original.png").read_bytes()
    pipeline.pause_first = True
    first = asyncio.create_task(pipeline.run("A"))
    try:
        await bounded(pipeline.first_render.wait(), timeout=5)
        async with pipeline.database() as session:
            setter_pid = await bounded(session.scalar(text("SELECT pg_backend_pid()")))
            assert setter_pid not in {pipeline.pids["A"], pipeline.observer_pid}
            current_id = await bounded(session.scalar(select(ProductImageVariant.id).where(
                ProductImageVariant.id != previous_id)))
            pipeline.wait_storage_until = (await bounded(session.execute(update(ProductImageVariant)
                .where(ProductImageVariant.id == current_id)
                .values(lease_expires_at=text("clock_timestamp() + interval '2 seconds'"))
                .returning(ProductImageVariant.lease_expires_at)))).scalar_one()
            await bounded(session.commit())
    finally:
        pipeline.release_first.set()
        expired = await bounded(first)
    assert expired.error is None and expired.counts == {"skipped": 1}
    assert pipeline.storage_crossed_deadline and pipeline.output_flush_succeeded
    assert len(pipeline.saved) == 1 and not Path(pipeline.saved[0]["file_path"]).exists()
    assert await variant_snapshot(pipeline.database, previous_id) == previous
    async with pipeline.database() as session:
        current = await bounded(session.get(ProductImageVariant, current_id))
        assert current.status == "processing" and current.output_upload_id is None and not current.is_preferred
        assert current.attempt_count == 1
        assert len(list(await bounded(session.scalars(select(Upload))))) == 1
    reclaimed = await bounded(pipeline.run("B"))
    assert reclaimed.error is None and reclaimed.counts == {"processed": 1}
    async with pipeline.database() as session:
        current = await bounded(session.get(ProductImageVariant, current_id))
        assert current.status == "succeeded" and current.is_preferred and current.attempt_count == 2
        assert current.attempt_token is None and current.lease_expires_at is None
        assert (await bounded(session.get(Upload, current.output_upload_id))).file_path == pipeline.saved[1]["file_path"]
        assert not (await bounded(session.get(ProductImageVariant, previous_id))).is_preferred
        assert len(list(await bounded(session.scalars(select(Upload))))) == 2
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert Path(pipeline.saved[1]["file_path"]).is_file()
    assert (pipeline.root / "original.png").read_bytes() == original and (pipeline.root / "previous.png").is_file()


async def test_deferred_unique_rejection_at_real_commit_cleans_owned_output_and_preserves_previous(image_pipeline):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    previous = await variant_snapshot(pipeline.database, previous_id)
    original = (pipeline.root / "original.png").read_bytes()
    async with pipeline.database() as session:
        await bounded(session.execute(text(
            "ALTER TABLE uploads ADD CONSTRAINT uq_synthetic_attempt_filename "
            "UNIQUE (file_name) DEFERRABLE INITIALLY DEFERRED")))
        await bounded(session.commit())
    pipeline.duplicate_upload_name = "previous.png"
    result = await bounded(pipeline.run("A"))
    assert pipeline.output_flush_succeeded
    assert isinstance(result.error, DBAPIError) and result.error is pipeline.output_commit_error
    assert result.error.orig.sqlstate == "23505"
    assert getattr(result.error.orig.__cause__, "constraint_name", None) == "uq_synthetic_attempt_filename"
    assert result.counts["processed"] == 0
    assert len(pipeline.saved) == 1 and not Path(pipeline.saved[0]["file_path"]).exists()
    assert await variant_snapshot(pipeline.database, previous_id) == previous
    async with pipeline.database() as session:
        assert len(list(await bounded(session.scalars(select(Upload))))) == 1
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert (pipeline.root / "original.png").read_bytes() == original and (pipeline.root / "previous.png").is_file()


async def test_rollback_acknowledgement_loss_preserves_primary_flush_error_and_retains_output(image_pipeline, caplog):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    previous = await variant_snapshot(pipeline.database, previous_id)
    original = (pipeline.root / "original.png").read_bytes()
    pipeline.bad_upload = True
    pipeline.rollback_fault = OSError("Synthetic rollback acknowledgement loss after actual rollback")
    result = await bounded(pipeline.run("A"))
    assert pipeline.rollback_fault_injected
    assert isinstance(result.error, DBAPIError) and result.error.orig.sqlstate == "23502"
    assert result.error is not pipeline.rollback_fault and result.counts["processed"] == 0
    assert len(pipeline.saved) == 1 and Path(pipeline.saved[0]["file_path"]).is_file()
    assert "Image-attempt rollback failed; retaining the new output" in caplog.text
    assert await variant_snapshot(pipeline.database, previous_id) == previous
    async with pipeline.database() as session:
        assert len(list(await bounded(session.scalars(select(Upload))))) == 1
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert (pipeline.root / "original.png").read_bytes() == original and (pipeline.root / "previous.png").is_file()


async def test_publication_task_cancel_after_real_upload_flush_rolls_back_and_cleans_output(image_pipeline):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    previous = await variant_snapshot(pipeline.database, previous_id)
    original = (pipeline.root / "original.png").read_bytes()
    pipeline.cancel_after_flush = True
    with pytest.raises(asyncio.CancelledError):
        await bounded(pipeline.run("A"))
    assert pipeline.cancel_after_flush_injected and pipeline.output_flush_succeeded
    assert len(pipeline.saved) == 1 and not Path(pipeline.saved[0]["file_path"]).exists()
    assert await variant_snapshot(pipeline.database, previous_id) == previous
    async with pipeline.database() as session:
        current = (await bounded(session.scalars(select(ProductImageVariant).where(
            ProductImageVariant.id != previous_id)))).one()
        assert current.status == "processing" and current.attempt_count == 1
        assert current.output_upload_id is None and current.output_url is None and not current.is_preferred
        assert current.attempt_token and current.lease_expires_at
        assert len(list(await bounded(session.scalars(select(Upload))))) == 1
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert (pipeline.root / "original.png").read_bytes() == original and (pipeline.root / "previous.png").is_file()


async def test_cancelled_commit_acknowledgement_keeps_durable_output_and_replay_skips(image_pipeline):
    pipeline = image_pipeline
    previous_id = await pipeline.seed_variant(status="succeeded", previous=True)
    original = (pipeline.root / "original.png").read_bytes()
    pipeline.commit_fault = asyncio.CancelledError("Synthetic cancellation after actual output commit")
    with pytest.raises(asyncio.CancelledError) as caught:
        await bounded(pipeline.run("A"))
    assert caught.value.args == pipeline.commit_fault.args and pipeline.commit_fault_injected
    assert pipeline.output_flush_succeeded
    assert len(pipeline.saved) == 1 and Path(pipeline.saved[0]["file_path"]).is_file()
    async with pipeline.database() as session:
        current = (await bounded(session.scalars(select(ProductImageVariant).where(
            ProductImageVariant.id != previous_id)))).one()
        current_id = current.id
        assert current.status == "succeeded" and current.is_preferred and current.error_detail is None
        assert current.attempt_token is None and current.lease_expires_at is None
        upload = await bounded(session.get(Upload, current.output_upload_id))
        assert upload.file_path == pipeline.saved[0]["file_path"] and current.output_url == upload.file_url
        assert not (await bounded(session.get(ProductImageVariant, previous_id))).is_preferred
        assert len(list(await bounded(session.scalars(select(Upload))))) == 2
    winner = await variant_snapshot(pipeline.database, current_id)
    pipeline.commit_fault = None
    replay = await bounded(pipeline.run("B"))
    assert replay.error is None and replay.counts == {"skipped": 1}
    assert await variant_snapshot(pipeline.database, current_id) == winner
    assert pipeline.calls == ["A"] and len(pipeline.saved) == 1
    assert Path(pipeline.saved[0]["file_path"]).is_file()
    assert (pipeline.root / "original.png").read_bytes() == original and (pipeline.root / "previous.png").is_file()
