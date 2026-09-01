from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Master
from app.models.product import Product
from app.models.shop import ProductImage
from app.models.upload import Upload
from app.services.uploads import delete_upload_file, save_image_upload

PRODUCT_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


class ProductImageService:
    async def _get_product(self, session: AsyncSession, product_id: int) -> Product:
        product = await session.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
        return product

    async def _get_image(self, session: AsyncSession, product_id: int, image_id: int) -> ProductImage:
        result = await session.execute(
            select(ProductImage).where(
                ProductImage.id == image_id,
                ProductImage.product_id == product_id,
            )
        )
        image = result.scalar_one_or_none()
        if image is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product image not found")
        return image

    async def list_images(self, session: AsyncSession, product_id: int) -> list[ProductImage]:
        await self._get_product(session, product_id)
        result = await session.execute(
            select(ProductImage)
            .where(ProductImage.product_id == product_id)
            .order_by(ProductImage.sort_order.asc(), ProductImage.id.asc())
        )
        return list(result.scalars().all())

    async def create_image(
        self,
        session: AsyncSession,
        product_id: int,
        file: UploadFile,
        alt: str | None = None,
    ) -> ProductImage:
        await self._get_product(session, product_id)
        upload_data = await save_image_upload(
            file,
            folder=f"products/{product_id}",
            allowed_formats=PRODUCT_IMAGE_FORMATS,
        )
        try:
            upload = Upload(**upload_data)
            session.add(upload)
            await session.flush()

            max_sort_order = (
                await session.execute(
                    select(func.max(ProductImage.sort_order)).where(ProductImage.product_id == product_id)
                )
            ).scalar_one()
            image = ProductImage(
                product_id=product_id,
                upload_id=upload.id,
                image_url=upload.file_url,
                alt=alt,
                sort_order=(max_sort_order if max_sort_order is not None else -1) + 1,
                is_active=True,
            )
            session.add(image)
            await session.commit()
        except Exception:
            await session.rollback()
            delete_upload_file(upload_data["file_path"])
            raise
        await session.refresh(image)
        return image

    async def replace_file(
        self,
        session: AsyncSession,
        product_id: int,
        image_id: int,
        file: UploadFile,
    ) -> ProductImage:
        await self._get_product(session, product_id)
        image = await self._get_image(session, product_id, image_id)
        old_upload = await session.get(Upload, image.upload_id) if image.upload_id is not None else None
        upload_data = await save_image_upload(
            file,
            folder=f"products/{product_id}",
            allowed_formats=PRODUCT_IMAGE_FORMATS,
        )
        old_file_path: str | None = None
        try:
            upload = Upload(**upload_data)
            session.add(upload)
            await session.flush()

            image.upload_id = upload.id
            image.image_url = upload.file_url
            await session.flush()

            if old_upload is not None and not await self._upload_is_referenced(session, old_upload.id):
                old_file_path = old_upload.file_path
                await session.delete(old_upload)
            await session.commit()
        except Exception:
            await session.rollback()
            delete_upload_file(upload_data["file_path"])
            raise
        if old_file_path:
            delete_upload_file(old_file_path)
        await session.refresh(image)
        return image

    async def update_image(
        self,
        session: AsyncSession,
        product_id: int,
        image_id: int,
        data: Mapping[str, object],
    ) -> ProductImage:
        await self._get_product(session, product_id)
        image = await self._get_image(session, product_id, image_id)
        for key in ("alt", "is_active"):
            if key in data:
                setattr(image, key, data[key])
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(image)
        return image

    async def reorder_images(
        self,
        session: AsyncSession,
        product_id: int,
        image_ids: list[int],
    ) -> list[ProductImage]:
        await self._get_product(session, product_id)
        result = await session.execute(
            select(ProductImage).where(ProductImage.product_id == product_id)
        )
        images = list(result.scalars().all())
        if len(image_ids) != len(set(image_ids)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="image_ids must be unique")
        image_by_id = {image.id: image for image in images}
        if set(image_ids) != set(image_by_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="image_ids must contain every image belonging to the product",
            )
        for sort_order, image_id in enumerate(image_ids):
            image_by_id[image_id].sort_order = sort_order
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return [image_by_id[image_id] for image_id in image_ids]

    async def delete_image(self, session: AsyncSession, product_id: int, image_id: int) -> None:
        await self._get_product(session, product_id)
        image = await self._get_image(session, product_id, image_id)
        upload = await session.get(Upload, image.upload_id) if image.upload_id is not None else None
        file_path: str | None = None
        try:
            await session.delete(image)
            await session.flush()

            if upload is not None and not await self._upload_is_referenced(session, upload.id):
                file_path = upload.file_path
                await session.delete(upload)

            result = await session.execute(
                select(ProductImage)
                .where(ProductImage.product_id == product_id)
                .order_by(ProductImage.sort_order.asc(), ProductImage.id.asc())
            )
            remaining_images = list(result.scalars().all())
            for sort_order, remaining_image in enumerate(remaining_images):
                remaining_image.sort_order = sort_order

            await session.commit()
        except Exception:
            await session.rollback()
            raise
        if file_path:
            delete_upload_file(file_path)

    async def _upload_is_referenced(self, session: AsyncSession, upload_id: int) -> bool:
        product_reference = await session.execute(
            select(ProductImage.id).where(ProductImage.upload_id == upload_id).limit(1)
        )
        if product_reference.scalar_one_or_none() is not None:
            return True

        master_reference = await session.execute(
            select(Master.id)
            .where(or_(Master.photo_upload_id == upload_id, Master.avatar_upload_id == upload_id))
            .limit(1)
        )
        return master_reference.scalar_one_or_none() is not None


product_image_service = ProductImageService()
