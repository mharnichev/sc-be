"""Process complete synthetic image attempts without disk, DB, or network I/O."""
from __future__ import annotations

from argparse import Namespace
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

from app.models.product import Product
from app.models.shop import ProductImageVariant
from app.models.upload import Upload
from app.services.studio_product_composer import ComposerError, ComposerRender
from app.utils import process_product_images as processing


def image_bytes(image_format="PNG", *, color=(40, 80, 120)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 3), color=color).save(output, format=image_format)
    return output.getvalue()


def cli_options(**changes) -> Namespace:
    return Namespace(**{
        "dry_run": False, "retry_failed": False,
        "composer_url": "http://127.0.0.1:8765", "request_timeout": 60,
        "poll_seconds": 1, "max_wait_seconds": 900,
        "removal_quality": "high", "resolution": "4:5",
        "output_format": "webp", "output_quality": 90,
        **changes,
    })


def output_config(args) -> dict:
    return {
        "background_preset": processing.PRESET,
        "removal_quality": args.removal_quality,
        "resolution": args.resolution,
        "output_format": args.output_format,
        "output_quality": args.output_quality,
    }


def matches(row, expression) -> bool:
    if expression is None:
        return True
    if isinstance(expression, BooleanClauseList):
        assert expression.operator is operators.and_
        return all(matches(row, clause) for clause in expression.clauses)
    assert isinstance(expression, BinaryExpression)
    left = getattr(row, expression.left.name)
    right = expression.right.value
    if expression.operator is operators.eq:
        return left == right
    assert expression.operator is operators.ne
    return left != right


class Result:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        self._snapshot()
        return self

    async def __aexit__(self, *args):
        await self.rollback()

    def _snapshot(self):
        self.variant_rows = list(self.store.variants)
        self.upload_rows = dict(self.store.uploads)
        self.row_values = [(row, {column.key: deepcopy(getattr(row, column.key))
                                 for column in row.__table__.columns})
                           for row in [*self.variant_rows, *self.upload_rows.values()]]

    async def rollback(self):
        self.store.variants[:] = self.variant_rows
        self.store.uploads.clear()
        self.store.uploads.update(self.upload_rows)
        for row, values in self.row_values:
            for key, value in values.items():
                setattr(row, key, deepcopy(value))

    async def scalar(self, statement):
        assert "clock_timestamp()" in str(statement)
        return datetime.now(UTC)

    async def execute(self, statement):
        if any(description.get("entity") is Product for description in getattr(statement, "column_descriptions", ())):
            return Result([self.store.source.product_id])
        rows = [row for row in self.store.variants if matches(row, statement.whereclause)]
        if isinstance(statement, Update):
            values = {
                getattr(key, "key", str(key).rsplit(".", 1)[-1]): value.value
                for key, value in statement._values.items()
            }
            assert values == {"is_preferred": False}
            for row in rows:
                row.is_preferred = False
            return Result([])
        return Result(sorted(rows, key=lambda row: row.created_at, reverse=True))

    async def get(self, model, identifier):
        assert model is Upload
        return self.store.uploads.get(identifier)

    def add(self, instance):
        if isinstance(instance, ProductImageVariant):
            instance.id = len(self.store.variants) + 1
            instance.created_at = datetime(2026, 9, 6, tzinfo=UTC) + timedelta(seconds=instance.id)
            self.store.variants.append(instance)
        else:
            assert isinstance(instance, Upload)
            instance.id = len(self.store.uploads) + 1
            self.store.uploads[instance.id] = instance

    async def commit(self):
        keys = [
            (row.product_id, row.source_key, row.source_fingerprint, row.preset, row.recipe_version)
            for row in self.store.variants
        ]
        assert len(keys) == len(set(keys)), "Persisted recipe identities must satisfy the existing unique constraint"
        assert all(len(row.recipe_version) <= 64 for row in self.store.variants)
        self.store.commits += 1
        self._snapshot()

    async def refresh(self, instance):
        return None

    async def flush(self):
        return None


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def pipeline(monkeypatch):
    source = processing.SourceImage(7, 3, "gallery:3", "/media/products/original.png")
    store = SimpleNamespace(
        source=source, original=image_bytes(), variants=[], uploads={}, files={},
        calls=[], commits=0, fail_next=False,
    )

    async def materialize(actual):
        assert actual == source
        return Path("/virtual/original.png"), sha256(store.original).hexdigest(), None

    async def save(file, *, folder, allowed_formats):
        content = await file.read()
        with Image.open(BytesIO(content)) as image:
            image_format = image.format
        mime, suffix = {"PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp"), "JPEG": ("image/jpeg", ".jpg")}[image_format]
        assert image_format in allowed_formats
        assert file.content_type == mime
        name = f"output-{len(store.files) + 1}{suffix}"
        file_path = f"/virtual/{folder}/{name}"
        store.files[file_path] = content
        return {"file_name": name, "file_path": file_path, "file_url": f"/media/{folder}/{name}", "content_type": mime, "size": len(content)}

    class Composer:
        def __init__(self, *args, **kwargs):
            pass

        async def render_studio_light(self, path, **options):
            assert str(path) == "/virtual/original.png"
            store.calls.append(options)
            if store.fail_next:
                store.fail_next = False
                raise ComposerError("Synthetic render failure")
            image_format = {"webp": "WEBP", "png": "PNG", "jpg": "JPEG"}[options["output_format"]]
            return ComposerRender(image=image_bytes(image_format), processor_version="fake-composer-v1", audit={"project_id": "synthetic"})

    monkeypatch.setattr(processing, "_materialize_source", materialize)
    monkeypatch.setattr(processing, "AsyncSessionLocal", lambda: Session(store))
    monkeypatch.setattr(processing, "StudioProductComposerClient", Composer)
    monkeypatch.setattr(processing, "save_image_upload", save)
    monkeypatch.setattr(processing, "Path", lambda path: SimpleNamespace(is_file=lambda: str(path) in store.files))
    return store


async def process(store, **changes) -> Counter:
    counts = Counter()
    await processing.process_source(store.source, cli_options(**changes), counts)
    return counts


def immutable_output(row) -> tuple:
    return (row.source_fingerprint, row.recipe_version, row.output_upload_id, row.output_url,
            row.processing_config, row.status, row.attempt_count)


@pytest.mark.anyio
async def test_same_output_options_skip_without_new_rows_or_render(pipeline):
    assert await process(pipeline) == {"processed": 1}
    first = immutable_output(pipeline.variants[0])
    assert await process(pipeline) == {"skipped": 1}
    assert len(pipeline.calls) == len(pipeline.variants) == len(pipeline.uploads) == 1
    assert immutable_output(pipeline.variants[0]) == first


@pytest.mark.anyio
@pytest.mark.parametrize(("option", "value"), [
    ("removal_quality", "ultra"), ("resolution", "1:1"),
    ("output_format", "png"), ("output_quality", 95),
])
async def test_each_output_option_renders_a_distinct_immutable_variant(pipeline, option, value):
    original = pipeline.original
    assert await process(pipeline) == {"processed": 1}
    first = pipeline.variants[0]
    snapshot = immutable_output(first)
    old_files = dict(pipeline.files)

    assert await process(pipeline, **{option: value}) == {"processed": 1}
    assert len(pipeline.variants) == len(pipeline.calls) == len(pipeline.uploads) == 2
    second = pipeline.variants[1]
    assert first.recipe_version != second.recipe_version
    assert immutable_output(first) == snapshot
    assert {row.id for row in pipeline.variants if row.is_preferred} == {second.id}
    assert {key: pipeline.files[key] for key in old_files} == old_files
    assert pipeline.original == original
    assert {key: second.processing_config[key] for key in output_config(cli_options())} == output_config(cli_options(**{option: value}))
    assert await process(pipeline, **{option: value}) == {"skipped": 1}
    assert len(pipeline.calls) == 2


def add_legacy(store, config):
    upload = Upload(id=1, file_name="legacy.webp", file_path="/virtual/legacy.webp", file_url="/media/legacy.webp", content_type="image/webp", size=1)
    store.uploads[upload.id] = upload
    store.files[upload.file_path] = image_bytes("WEBP")
    row = ProductImageVariant(
        id=1, product_id=store.source.product_id, source_image_id=store.source.source_image_id,
        source_key=store.source.source_key, source_url=store.source.source_url,
        source_fingerprint=sha256(store.original).hexdigest(), preset=processing.PRESET,
        recipe_version=processing.RECIPE_VERSION, status="succeeded", is_preferred=True,
        processing_config=config, output_upload_id=upload.id, output_url=upload.file_url,
        attempt_count=1, created_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    store.variants.append(row)
    return row


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_config", [None, "known-defaults", "different-options"])
async def test_explicit_invocation_keeps_legacy_output_and_creates_current_identity_once(pipeline, legacy_config):
    config = None if legacy_config is None else output_config(cli_options(output_quality=80 if legacy_config == "different-options" else 90))
    legacy = add_legacy(pipeline, config)
    snapshot = immutable_output(legacy)
    legacy_bytes = pipeline.files["/virtual/legacy.webp"]

    assert await process(pipeline) == {"processed": 1}
    assert len(pipeline.variants) == 2
    current = pipeline.variants[1]
    assert current.recipe_version != legacy.recipe_version
    assert immutable_output(legacy) == snapshot
    assert pipeline.files["/virtual/legacy.webp"] == legacy_bytes
    assert {row.id for row in pipeline.variants if row.is_preferred} == {current.id}
    assert await process(pipeline) == {"skipped": 1}
    assert len(pipeline.calls) == 1


@pytest.mark.anyio
async def test_dry_run_unknown_legacy_configuration_only_queues_work(pipeline):
    legacy = add_legacy(pipeline, None)
    snapshot = immutable_output(legacy)
    assert await process(pipeline, dry_run=True) == {"queued": 1}
    assert pipeline.calls == []
    assert pipeline.commits == 0
    assert len(pipeline.variants) == 1
    assert immutable_output(legacy) == snapshot


@pytest.mark.anyio
async def test_failed_attempt_requires_retry_flag_for_the_same_configuration(pipeline):
    pipeline.fail_next = True
    assert await process(pipeline) == {"failed": 1}
    failed = pipeline.variants[0]
    assert failed.status == "failed"
    assert failed.attempt_count == 1
    assert await process(pipeline) == {"skipped": 1}
    assert len(pipeline.calls) == 1
    assert await process(pipeline, retry_failed=True) == {"processed": 1}
    assert pipeline.variants == [failed]
    assert failed.status == "succeeded"
    assert failed.attempt_count == 2
    assert failed.is_preferred is True


@pytest.mark.anyio
async def test_changed_configuration_failure_does_not_mark_previous_success_failed(pipeline):
    assert await process(pipeline) == {"processed": 1}
    previous = pipeline.variants[0]
    snapshot = immutable_output(previous)
    pipeline.fail_next = True
    assert await process(pipeline, output_quality=95) == {"failed": 1}
    assert len(pipeline.variants) == 2
    assert immutable_output(previous) == snapshot
    assert previous.is_preferred is True
    assert pipeline.variants[1].status == "failed"
    assert pipeline.variants[1].is_preferred is False


@pytest.mark.anyio
async def test_changed_source_fingerprint_still_creates_new_variant(pipeline):
    assert await process(pipeline) == {"processed": 1}
    old = pipeline.variants[0]
    old_output = immutable_output(old)
    pipeline.original = image_bytes(color=(120, 80, 40))
    assert await process(pipeline) == {"processed": 1}
    assert len(pipeline.variants) == 2
    assert old.source_fingerprint != pipeline.variants[1].source_fingerprint
    assert immutable_output(old) == old_output
    assert {row.id for row in pipeline.variants if row.is_preferred} == {pipeline.variants[1].id}


@pytest.mark.anyio
async def test_transport_timing_and_endpoint_do_not_change_output_identity(pipeline):
    assert await process(pipeline) == {"processed": 1}
    assert await process(pipeline, composer_url="http://localhost:9000", request_timeout=120, poll_seconds=2, max_wait_seconds=1200) == {"skipped": 1}
    assert len(pipeline.calls) == len(pipeline.variants) == 1


@pytest.mark.anyio
async def test_recipe_revision_creates_new_identity_and_replaces_preferred_flag(pipeline, monkeypatch):
    assert await process(pipeline) == {"processed": 1}
    old = pipeline.variants[0]
    old_output = immutable_output(old)
    monkeypatch.setattr(processing, "RECIPE_VERSION", "studio-light-v2")
    assert await process(pipeline) == {"processed": 1}
    assert len(pipeline.variants) == 2
    assert old.recipe_version != pipeline.variants[1].recipe_version
    assert immutable_output(old) == old_output
    assert {row.id for row in pipeline.variants if row.is_preferred} == {pipeline.variants[1].id}
