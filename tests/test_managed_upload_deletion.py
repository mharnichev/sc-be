"""Managed-file containment and upload sharing in synthetic, isolated storage."""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from PIL import Image
from sqlalchemy import select
from starlette.datastructures import Headers

from app.core.config import settings
from app.models.booking import Master
from app.models.product import Product
from app.models.shop import ProductImage, ProductImageVariant
from app.models.upload import Upload
from app.services import product_images as image_module
from app.services.product_images import ProductImageService
from app.services.uploads import delete_upload_file
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


@pytest.fixture
def managed_root(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(settings, "upload_dir", str(root))
    monkeypatch.setattr(settings, "upload_url_prefix", "/media")
    monkeypatch.setattr(settings, "max_upload_size_bytes", 5 * 1024 * 1024)
    return root


@pytest.mark.parametrize(
    "kind", ["absolute", "traversal", "sibling-prefix", "file-symlink", "directory-symlink", "cycle"]
)
def test_unsafe_file_paths_are_retained_without_any_unlink(managed_root, tmp_path, monkeypatch, kind):
    outside = tmp_path / "uploads-other" / "keep.png"
    outside.parent.mkdir()
    outside.write_bytes(b"synthetic outside file")
    if kind == "absolute":
        candidate = outside
    elif kind == "traversal":
        candidate = managed_root / ".." / "uploads-other" / outside.name
    elif kind == "sibling-prefix":
        candidate = Path(str(managed_root) + "-other") / outside.name
    elif kind == "file-symlink":
        candidate = managed_root / "escaped.png"
        candidate.symlink_to(outside)
    elif kind == "directory-symlink":
        link = managed_root / "escaped-directory"
        link.symlink_to(outside.parent, target_is_directory=True)
        candidate = link / outside.name
    else:
        candidate = managed_root / "cycle-a"
        other = managed_root / "cycle-b"
        candidate.symlink_to(other)
        other.symlink_to(candidate)

    attempts = []

    def forbidden_unlink(path, *args, **kwargs):
        attempts.append(path)
        raise AssertionError("Unsafe paths must be rejected before unlink")

    monkeypatch.setattr(Path, "unlink", forbidden_unlink)
    delete_upload_file(str(candidate))
    assert attempts == []
    assert outside.read_bytes() == b"synthetic outside file"
    if kind in {"file-symlink", "cycle"}:
        assert candidate.is_symlink()
    if kind == "directory-symlink":
        assert candidate.parent.is_symlink()


@pytest.mark.parametrize("kind", ["absolute", "stored-relative", "relative-root", "symlink-root", "contained-symlink"])
def test_legitimate_managed_paths_are_removed(managed_root, tmp_path, monkeypatch, kind):
    target = managed_root / "products" / "keep.png"
    target.parent.mkdir()
    target.write_bytes(b"synthetic managed file")
    candidate = target
    if kind in {"stored-relative", "relative-root"}:
        monkeypatch.chdir(tmp_path)
        candidate = Path("uploads/products/keep.png")
        if kind == "relative-root":
            monkeypatch.setattr(settings, "upload_dir", "uploads")
    elif kind == "symlink-root":
        alias = tmp_path / "root-alias"
        alias.symlink_to(managed_root, target_is_directory=True)
        monkeypatch.setattr(settings, "upload_dir", str(alias))
        candidate = alias / "products" / "keep.png"
    elif kind == "contained-symlink":
        candidate = managed_root / "contained.png"
        candidate.symlink_to(target)

    delete_upload_file(str(candidate))
    assert not candidate.is_symlink()
    assert not candidate.exists()
    if kind == "contained-symlink":
        # unlink retains its original semantics: remove the link, not its target.
        assert target.read_bytes() == b"synthetic managed file"
    else:
        assert not target.exists()


@pytest.mark.parametrize("value", [None, "", "missing"])
def test_empty_or_missing_managed_file_is_a_noop(managed_root, value):
    file_path = str(managed_root / "missing.png") if value == "missing" else value
    delete_upload_file(file_path)
    assert list(managed_root.iterdir()) == []


def replacement_file():
    data = BytesIO()
    Image.new("RGB", (3, 3), (40, 80, 120)).save(data, format="PNG")
    data.seek(0)
    return UploadFile(file=data, filename="replacement.png", headers=Headers({"content-type": "image/png"}))


async def seed_gallery(database, old_file, *, shared_role=None):
    async with database() as session:
        product = Product(name="Managed deletion product", slug="managed-deletion-product", price=Decimal("100.00"))
        upload = Upload(file_name=old_file.name, file_path=str(old_file), file_url="/media/old.png", size=old_file.stat().st_size)
        session.add_all([product, upload])
        await session.flush()
        image = ProductImage(product_id=product.id, upload_id=upload.id, image_url=upload.file_url, sort_order=0)
        session.add(image)
        await session.flush()
        reference = None
        reference_field = None
        if shared_role == "gallery":
            reference = ProductImage(product_id=product.id, upload_id=upload.id, image_url=upload.file_url, sort_order=1)
            reference_field = "upload_id"
        elif shared_role == "variant":
            reference = ProductImageVariant(
                product_id=product.id, source_key="synthetic-legacy-source", source_url="/media/source.png",
                source_fingerprint="a" * 64, preset="synthetic", recipe_version="1", status="succeeded",
                output_upload_id=upload.id, output_url=upload.file_url,
            )
            reference_field = "output_upload_id"
        elif shared_role is not None:
            reference = Master(full_name="Synthetic upload owner", **{shared_role: upload.id})
            reference_field = shared_role
        if reference is not None:
            session.add(reference)
        await session.commit()
        return SimpleNamespace(
            product_id=product.id, image_id=image.id, upload_id=upload.id,
            reference_model=type(reference) if reference is not None else None,
            reference_id=reference.id if reference is not None else None, reference_field=reference_field,
        )


async def perform_operation(session, seed, operation):
    service = ProductImageService()
    if operation == "delete":
        await service.delete_image(session, seed.product_id, seed.image_id)
    else:
        file = replacement_file()
        try:
            await service.replace_file(session, seed.product_id, seed.image_id, file)
        finally:
            await file.close()


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["delete", "replace"])
@pytest.mark.parametrize("role", ["gallery", "variant", "photo_upload_id", "avatar_upload_id", "passport_photo_upload_id"])
async def test_every_upload_fk_owner_retains_shared_row_and_file(database, managed_root, monkeypatch, operation, role):
    old_file = managed_root / "old.png"
    old_file.write_bytes(b"synthetic shared old file")
    seed = await seed_gallery(database, old_file, shared_role=role)
    deleted_paths = []

    def observe_delete(path):
        deleted_paths.append(path)
        delete_upload_file(path)

    monkeypatch.setattr(image_module, "delete_upload_file", observe_delete)
    async with database() as session:
        await perform_operation(session, seed, operation)
    async with database() as session:
        assert await session.get(Upload, seed.upload_id) is not None
        reference = await session.get(seed.reference_model, seed.reference_id)
        assert reference is not None
        assert getattr(reference, seed.reference_field) == seed.upload_id
        image = await session.get(ProductImage, seed.image_id)
        if operation == "delete":
            assert image is None
        else:
            assert image.upload_id != seed.upload_id
            new_upload = await session.get(Upload, image.upload_id)
            assert Path(new_upload.file_path).is_file()
    assert str(old_file) not in deleted_paths
    assert old_file.read_bytes() == b"synthetic shared old file"


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["delete", "replace"])
@pytest.mark.parametrize("alias_referenced", [False, True], ids=["metadata-only-alias", "gallery-alias"])
async def test_another_upload_id_with_exact_same_stored_path_preserves_file_and_rows(
    database, managed_root, monkeypatch, operation, alias_referenced,
):
    old_file = managed_root / "old.png"
    old_file.write_bytes(b"synthetic exact stored path alias")
    seed = await seed_gallery(database, old_file)
    async with database() as session:
        alias = Upload(file_name="alias.png", file_path=str(old_file), file_url="/media/alias.png")
        session.add(alias)
        await session.flush()
        alias_image = None
        if alias_referenced:
            alias_image = ProductImage(
                product_id=seed.product_id, upload_id=alias.id, image_url=alias.file_url, sort_order=1,
            )
            session.add(alias_image)
        await session.commit()
        alias_id = alias.id
        alias_image_id = alias_image.id if alias_image is not None else None
    deleted_paths = []

    def observe_delete(path):
        deleted_paths.append(path)
        delete_upload_file(path)

    monkeypatch.setattr(image_module, "delete_upload_file", observe_delete)
    async with database() as session:
        await perform_operation(session, seed, operation)
    async with database() as session:
        assert await session.get(Upload, seed.upload_id) is not None
        alias = await session.get(Upload, alias_id)
        assert alias is not None
        assert alias.file_path == str(old_file)
        if alias_image_id is not None:
            alias_image = await session.get(ProductImage, alias_image_id)
            assert alias_image.upload_id == alias_id
    assert str(old_file) not in deleted_paths
    assert old_file.read_bytes() == b"synthetic exact stored path alias"


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["delete", "replace"])
async def test_unreferenced_managed_file_is_removed_only_after_successful_commit(database, managed_root, monkeypatch, operation):
    old_file = managed_root / "old.png"
    old_file.write_bytes(b"synthetic unreferenced old file")
    seed = await seed_gallery(database, old_file)
    committed = False
    deleted_paths = []

    def observe_delete(path):
        assert committed, "Old files must survive until commit completes"
        deleted_paths.append(path)
        delete_upload_file(path)

    monkeypatch.setattr(image_module, "delete_upload_file", observe_delete)
    async with database() as session:
        original_commit = session.commit

        async def record_commit():
            nonlocal committed
            assert old_file.exists()
            await original_commit()
            committed = True

        monkeypatch.setattr(session, "commit", record_commit)
        await perform_operation(session, seed, operation)
    assert deleted_paths == [str(old_file)]
    assert not old_file.exists()
    async with database() as session:
        assert await session.get(Upload, seed.upload_id) is None
        image = await session.get(ProductImage, seed.image_id)
        if operation == "delete":
            assert image is None
            assert list(managed_root.rglob("*")) == []
        else:
            assert image.upload_id != seed.upload_id
            upload = await session.get(Upload, image.upload_id)
            assert Path(upload.file_path).is_file()


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["delete", "replace"])
@pytest.mark.parametrize("failure_stage", ["flush", "commit"])
async def test_failure_before_commit_preserves_old_rows_and_file(database, managed_root, monkeypatch, operation, failure_stage):
    old_file = managed_root / "old.png"
    old_file.write_bytes(b"synthetic rollback old file")
    seed = await seed_gallery(database, old_file)
    deleted_paths = []

    def observe_delete(path):
        deleted_paths.append(path)
        assert path != str(old_file), "Rollback must not delete the old file"
        delete_upload_file(path)

    monkeypatch.setattr(image_module, "delete_upload_file", observe_delete)
    async with database() as session:
        async def reject_transaction(*args, **kwargs):
            raise RuntimeError("synthetic rejection before commit")

        monkeypatch.setattr(session, failure_stage, reject_transaction)
        with pytest.raises(RuntimeError, match="synthetic rejection before commit"):
            await perform_operation(session, seed, operation)
    assert old_file.read_bytes() == b"synthetic rollback old file"
    assert str(old_file) not in deleted_paths
    if operation == "delete":
        assert deleted_paths == []
    else:
        assert len(deleted_paths) == 1
        assert not Path(deleted_paths[0]).exists()
    async with database() as session:
        image = await session.get(ProductImage, seed.image_id)
        assert image is not None
        assert image.upload_id == seed.upload_id
        assert await session.get(Upload, seed.upload_id) is not None
        assert list(await session.scalars(select(Upload.id))) == [seed.upload_id]


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["delete", "replace"])
async def test_unknown_legacy_file_outside_managed_root_is_retained(database, managed_root, tmp_path, operation):
    old_file = tmp_path / "unknown-legacy.png"
    old_file.write_bytes(b"synthetic unknown legacy file")
    seed = await seed_gallery(database, old_file)
    async with database() as session:
        await perform_operation(session, seed, operation)
    assert old_file.read_bytes() == b"synthetic unknown legacy file"
