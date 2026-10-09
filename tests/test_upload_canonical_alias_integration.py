"""Distinct upload paths that resolve to one managed file must retain it."""
from __future__ import annotations

import pytest

from app.models.shop import ProductImage
from app.models.upload import Upload
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_managed_upload_deletion import managed_root, perform_operation, seed_gallery  # noqa: F401

pytestmark = pytest.mark.anyio


def alias_path(kind, managed_root, old_file, tmp_path, monkeypatch):
    if kind == "relative":
        monkeypatch.chdir(tmp_path)
        return "uploads/old.png"
    if kind == "dot":
        return f"{managed_root}/./old.png"
    if kind == "parent":
        (managed_root / "subdirectory").mkdir()
        return f"{managed_root}/subdirectory/../old.png"
    if kind == "directory-symlink":
        alias = tmp_path / "other-upload-root"
        alias.symlink_to(managed_root, target_is_directory=True)
        return str(alias / old_file.name)
    alias = managed_root / "other-name.png"
    alias.symlink_to(old_file)
    return str(alias)


@pytest.mark.parametrize("operation", ["delete", "replace"])
@pytest.mark.parametrize("kind", ["relative", "dot", "parent", "directory-symlink", "file-symlink"])
async def test_shared_canonical_file_is_retained_for_other_gallery_upload(
    database, managed_root, tmp_path, monkeypatch, operation, kind,
):
    old_file = managed_root / "old.png"
    old_file.write_bytes(b"synthetic canonical shared file")
    seed = await seed_gallery(database, old_file)
    path = alias_path(kind, managed_root, old_file, tmp_path, monkeypatch)
    async with database() as session:
        alias = Upload(file_name="alias.png", file_path=path, file_url="/media/alias.png")
        session.add(alias)
        await session.flush()
        image = ProductImage(product_id=seed.product_id, upload_id=alias.id,
                             image_url=alias.file_url, sort_order=1)
        session.add(image)
        await session.commit()
        alias_id, image_id = alias.id, image.id
    async with database() as session:
        await perform_operation(session, seed, operation)
    assert old_file.exists(), "A different gallery still references this canonical file"
    assert old_file.read_bytes() == b"synthetic canonical shared file"
    async with database() as session:
        assert await session.get(Upload, seed.upload_id) is not None
        alias = await session.get(Upload, alias_id)
        assert alias is not None and alias.file_path == path
        assert (await session.get(ProductImage, image_id)).upload_id == alias_id
