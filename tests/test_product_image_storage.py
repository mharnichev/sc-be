from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.datastructures import Headers

from app.core.config import settings
from app.models.product import Product
from app.models.shop import ProductImage
from app.models.upload import Upload
from app.services.product_images import PRODUCT_IMAGE_FORMATS, ProductImageService
from app.services.uploads import save_image_upload


def image_bytes(image_format: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 3), color=(40, 80, 120)).save(output, format=image_format)
    return output.getvalue()


def upload_file(content: bytes, *, content_type: str, filename: str = "photo.bin") -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class ImageSession:
    def __init__(self, *, fail_flush: bool = False):
        self.product = Product(id=7, name="Product", slug="product", price=10)
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.fail_flush = fail_flush
        self.committed = False
        self.rolled_back = False

    async def get(self, model, entity_id):
        if model is Product:
            return self.product if entity_id == self.product.id else None
        return None

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        if self.fail_flush:
            raise RuntimeError("database failure")
        for instance in self.added:
            if isinstance(instance, Upload) and instance.id is None:
                instance.id = 100
            if isinstance(instance, ProductImage) and instance.id is None:
                instance.id = 200

    async def execute(self, _statement):
        return ScalarResult(None)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, _instance):
        return None

    async def delete(self, instance):
        self.deleted.append(instance)


@pytest.fixture
def upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_url_prefix", "/media")
    monkeypatch.setattr(settings, "max_upload_size_bytes", 5 * 1024 * 1024)
    return tmp_path


@pytest.mark.anyio
async def test_save_image_uses_actual_format_and_canonical_metadata(upload_root: Path) -> None:
    data = await save_image_upload(
        upload_file(image_bytes("PNG"), content_type="image/png", filename="photo.jpg"),
        folder="products/7",
    )

    assert data["content_type"] == "image/png"
    assert data["file_url"].startswith("/media/products/7/")
    assert data["file_path"].endswith(".png")
    assert Path(data["file_path"]).is_file()


@pytest.mark.anyio
async def test_save_image_rejects_invalid_bytes_and_mismatched_mime(upload_root: Path) -> None:
    with pytest.raises(HTTPException) as invalid:
        await save_image_upload(
            upload_file(b"not an image", content_type="image/jpeg"),
            folder="products/7",
        )
    assert invalid.value.status_code == 400

    with pytest.raises(HTTPException) as mismatch:
        await save_image_upload(
            upload_file(image_bytes("PNG"), content_type="image/jpeg"),
            folder="products/7",
        )
    assert mismatch.value.status_code == 400
    assert not list(upload_root.rglob("*.png"))


@pytest.mark.anyio
async def test_gif_is_supported_by_generic_storage_but_rejected_for_product_images(upload_root: Path) -> None:
    generic = await save_image_upload(
        upload_file(image_bytes("GIF"), content_type="image/gif"),
        folder="images",
    )
    assert generic["content_type"] == "image/gif"

    with pytest.raises(HTTPException) as product_error:
        await save_image_upload(
            upload_file(image_bytes("GIF"), content_type="image/gif"),
            folder="products/7",
            allowed_formats=PRODUCT_IMAGE_FORMATS,
        )
    assert product_error.value.status_code == 400


@pytest.mark.anyio
async def test_save_image_removes_oversized_file(upload_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_upload_size_bytes", 10)
    with pytest.raises(HTTPException) as error:
        await save_image_upload(
            upload_file(image_bytes("PNG"), content_type="image/png"),
            folder="products/7",
        )
    assert error.value.status_code == 413
    assert not [path for path in upload_root.rglob("*") if path.is_file()]


@pytest.mark.anyio
async def test_create_product_image_cleans_file_when_database_flush_fails(upload_root: Path) -> None:
    session = ImageSession(fail_flush=True)
    with pytest.raises(RuntimeError, match="database failure"):
        await ProductImageService().create_image(
            session,
            7,
            upload_file(image_bytes("JPEG"), content_type="image/jpeg", filename="photo.jpg"),
        )

    assert session.rolled_back is True
    assert not [path for path in upload_root.rglob("*") if path.is_file()]
