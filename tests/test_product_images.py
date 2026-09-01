from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

from app.api.v1.routes import products as product_routes
from app.core.config import settings
from app.models.product import Product
from app.models.shop import ProductImage
from app.models.upload import Upload
from app.schemas.product import ProductImageUpdate
from app.services.product_images import ProductImageService

NOW = datetime(2026, 8, 31, tzinfo=UTC)


def image_bytes(fmt: str = "PNG") -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 40, 60)).save(stream, format=fmt)
    return stream.getvalue()


def upload_file(content: bytes, *, filename: str = "photo.png", content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers={"content-type": content_type},
    )


def make_product(product_id: int = 1, *, images: list[ProductImage] | None = None) -> Product:
    return Product(
        id=product_id,
        name="Clipper",
        slug=f"clipper-{product_id}",
        price=Decimal("100.00"),
        stock_quantity=2,
        is_active=True,
        image_url="/legacy/main.jpg",
        attributes_json={"image_urls": ["/legacy/second.jpg"]},
        images=images or [],
        created_at=NOW,
        updated_at=NOW,
    )


def make_upload(upload_id: int, *, path: str = "/tmp/photo.png") -> Upload:
    return Upload(
        id=upload_id,
        file_name=Path(path).name,
        file_path=path,
        file_url=f"/media/products/{Path(path).name}",
        content_type="image/png",
        size=10,
        created_at=NOW,
    )


def make_image(
    image_id: int,
    *,
    product_id: int = 1,
    upload_id: int | None = 10,
    sort_order: int = 0,
    is_active: bool = True,
    alt: str | None = "Product photo",
    url: str | None = None,
) -> ProductImage:
    return ProductImage(
        id=image_id,
        product_id=product_id,
        upload_id=upload_id,
        image_url=url or f"/media/products/{image_id}.png",
        alt=alt,
        sort_order=sort_order,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


class ScalarResult:
    def __init__(self, *, values: list[Any] | None = None, scalar: Any = None) -> None:
        self.values = values or []
        self.scalar = scalar

    def scalars(self) -> ScalarResult:
        return self

    def all(self) -> list[Any]:
        return self.values

    def scalar_one(self) -> Any:
        return self.scalar

    def scalar_one_or_none(self) -> Any:
        return self.scalar


class ImageSession:
    def __init__(
        self,
        *,
        product: Product | None = None,
        image: ProductImage | None = None,
        uploads: dict[int, Upload] | None = None,
        execute_results: list[ScalarResult] | None = None,
        fail_on_commit: bool = False,
    ) -> None:
        self.product = product
        self.image = image
        self.uploads = uploads or {}
        self.execute_results = list(execute_results or [])
        self.fail_on_commit = fail_on_commit
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.statements: list[Any] = []
        self.committed = False
        self.rolled_back = False
        self.flushed = False

    async def get(self, model: type[Any], entity_id: int) -> Any:
        if model is Product:
            return self.product if self.product is not None and self.product.id == entity_id else None
        if model is ProductImage:
            return self.image if self.image is not None and self.image.id == entity_id else None
        if model is Upload:
            return self.uploads.get(entity_id)
        return None

    async def execute(self, statement: Any) -> ScalarResult:
        self.statements.append(statement)
        if not self.execute_results:
            raise AssertionError("Unexpected database query in ImageSession")
        return self.execute_results.pop(0)

    def add(self, entity: Any) -> None:
        self.added.append(entity)
        if isinstance(entity, Upload) and entity.id is None:
            entity.id = max(self.uploads, default=100) + 1
            self.uploads[entity.id] = entity
        if isinstance(entity, ProductImage) and entity.id is None:
            entity.id = max([self.image.id] if self.image is not None else [200]) + 1
        if getattr(entity, "created_at", None) is None:
            entity.created_at = NOW
        if getattr(entity, "updated_at", None) is None:
            entity.updated_at = NOW

    async def flush(self) -> None:
        self.flushed = True

    async def refresh(self, _entity: Any) -> None:
        return None

    async def commit(self) -> None:
        if self.fail_on_commit:
            raise RuntimeError("database unavailable")
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def delete(self, entity: Any) -> None:
        self.deleted.append(entity)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_create_first_and_multiple_images_appends_and_assigns_uploads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    product = make_product()
    session = ImageSession(product=product, execute_results=[ScalarResult(scalar=None), ScalarResult(scalar=0)])
    service = ProductImageService()

    first = await service.create_image(session, 1, upload_file(image_bytes(), filename="one.png"), "One")
    second = await service.create_image(session, 1, upload_file(image_bytes(), filename="two.png"), "Two")

    assert [first.sort_order, second.sort_order] == [0, 1]
    assert [first.alt, second.alt] == ["One", "Two"]
    assert all(item.upload_id is not None for item in session.added if isinstance(item, ProductImage))
    assert len([item for item in session.added if isinstance(item, Upload)]) == 2
    assert first.image_url and first.image_url.startswith("/media/products/1/")
    assert session.committed is True


@pytest.mark.anyio
async def test_replace_file_preserves_image_id_alt_and_sort_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    old_upload = make_upload(10, path=str(tmp_path / "old.png"))
    image = make_image(5, upload_id=10, sort_order=3, alt="Keep me")
    session = ImageSession(
        product=make_product(images=[image]),
        image=image,
        uploads={10: old_upload},
        execute_results=[
            ScalarResult(scalar=image),
            ScalarResult(scalar=None),
            ScalarResult(scalar=None),
        ],
    )

    replaced = await ProductImageService().replace_file(session, 1, 5, upload_file(image_bytes(), filename="new.png"))

    assert replaced.id == 5
    assert replaced.alt == "Keep me"
    assert replaced.sort_order == 3
    assert replaced.upload_id != 10
    assert replaced.image_url and replaced.image_url.endswith(".png")
    assert old_upload in session.deleted


@pytest.mark.anyio
async def test_patch_image_changes_only_requested_fields() -> None:
    image = make_image(5, sort_order=2, alt="Original")
    session = ImageSession(
        product=make_product(images=[image]),
        image=image,
        execute_results=[ScalarResult(scalar=image)],
    )

    payload = ProductImageUpdate(is_active=False)
    updated = await ProductImageService().update_image(session, 1, 5, payload.model_dump(exclude_unset=True))

    assert updated.is_active is False
    assert updated.alt == "Original"
    assert updated.sort_order == 2
    assert session.committed is True


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ("update", "delete", "replace"))
async def test_image_operations_reject_image_not_owned_by_product(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    session = ImageSession(
        product=make_product(product_id=1),
        execute_results=[ScalarResult(scalar=None)],
    )
    service = ProductImageService()

    with pytest.raises(HTTPException) as exc_info:
        if operation == "update":
            await service.update_image(session, 1, 99, {"alt": "Nope"})
        elif operation == "delete":
            await service.delete_image(session, 1, 99)
        else:
            await service.replace_file(session, 1, 99, upload_file(image_bytes()))

    assert exc_info.value.status_code == 404
    assert session.committed is False
    assert session.deleted == []
    assert not list(tmp_path.rglob("*"))


@pytest.mark.anyio
async def test_reorder_requires_complete_unique_ids_and_reindexes_from_zero() -> None:
    images = [make_image(1, sort_order=9), make_image(2, sort_order=3), make_image(3, sort_order=7)]
    session = ImageSession(product=make_product(images=images), execute_results=[ScalarResult(values=images)])

    reordered = await ProductImageService().reorder_images(session, 1, [3, 1, 2])

    assert [item.id for item in reordered] == [3, 1, 2]
    assert [item.sort_order for item in reordered] == [0, 1, 2]


@pytest.mark.anyio
@pytest.mark.parametrize("image_ids", ([1, 1, 2], [1], [1, 2, 999]))
async def test_reorder_rejects_duplicates_missing_or_foreign_ids(image_ids: list[int]) -> None:
    images = [make_image(1), make_image(2, sort_order=1)]
    session = ImageSession(product=make_product(images=images), execute_results=[ScalarResult(values=images)])

    with pytest.raises(HTTPException) as exc_info:
        await ProductImageService().reorder_images(session, 1, image_ids)

    assert exc_info.value.status_code == 400
    assert session.committed is False
    assert [item.sort_order for item in images] == [0, 1]


@pytest.mark.anyio
async def test_empty_gallery_accepts_empty_reorder() -> None:
    session = ImageSession(product=make_product(images=[]), execute_results=[ScalarResult(values=[])])

    assert await ProductImageService().reorder_images(session, 1, []) == []
    assert session.committed is True


@pytest.mark.anyio
async def test_delete_image_deletes_orphan_upload_and_normalizes_remaining_order() -> None:
    image = make_image(1, sort_order=0, upload_id=10)
    remaining = [make_image(2, sort_order=4, upload_id=11), make_image(3, sort_order=8, upload_id=12)]
    session = ImageSession(
        product=make_product(images=[image, *remaining]),
        image=image,
        uploads={10: make_upload(10), 11: make_upload(11), 12: make_upload(12)},
        execute_results=[
            ScalarResult(scalar=image),
            ScalarResult(scalar=None),
            ScalarResult(scalar=None),
            ScalarResult(values=remaining),
        ],
    )

    await ProductImageService().delete_image(session, 1, 1)

    assert image in session.deleted
    assert session.uploads[10] in session.deleted
    assert [item.sort_order for item in remaining] == [0, 1]
    assert session.committed is True


@pytest.mark.anyio
async def test_delete_image_keeps_upload_shared_by_another_product_image() -> None:
    image = make_image(1, sort_order=0, upload_id=10)
    remaining = [make_image(2, sort_order=1, upload_id=10)]
    shared_upload = make_upload(10)
    session = ImageSession(
        product=make_product(images=[image, *remaining]),
        image=image,
        uploads={10: shared_upload},
        execute_results=[
            ScalarResult(scalar=image),
            ScalarResult(scalar=22),
            ScalarResult(values=remaining),
        ],
    )

    await ProductImageService().delete_image(session, 1, 1)

    assert image in session.deleted
    assert shared_upload not in session.deleted
    assert session.committed is True


@pytest.mark.anyio
async def test_db_failure_cleans_new_file_and_rolls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    product = make_product()
    session = ImageSession(product=product, execute_results=[ScalarResult(scalar=None)], fail_on_commit=True)

    with pytest.raises(RuntimeError):
        await ProductImageService().create_image(session, 1, upload_file(image_bytes()), "Photo")

    assert session.rolled_back is True
    assert not list((tmp_path / "products" / "1").glob("*"))


def test_product_image_urls_uses_only_active_gallery_when_any_rows_exist() -> None:
    images = [
        make_image(2, sort_order=1, is_active=True, url="/gallery/second.png"),
        make_image(1, sort_order=0, is_active=False, url="/gallery/hidden.png"),
    ]
    product = make_product(images=images)

    assert product_routes.product_image_urls(product) == ["/gallery/second.png"]


def test_product_image_urls_does_not_revive_legacy_urls_when_all_rows_inactive() -> None:
    product = make_product(images=[make_image(1, is_active=False, url="/gallery/hidden.png")])

    assert product_routes.product_image_urls(product) == []
