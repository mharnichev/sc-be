"""Create immutable Studio Light variants for product images.

Run with ``--dry-run`` first.  The command never calls ProductImageService's
replace/delete paths and never alters original product image rows or files.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.product import Product
from app.models.shop import ProductImageVariant
from app.models.upload import Upload
from app.services.product_images import PRODUCT_IMAGE_FORMATS
from app.services.studio_product_composer import ComposerError, StudioProductComposerClient
from app.services.uploads import save_image_upload

PRESET = "studio_light"
RECIPE_VERSION = "studio-light-v1"


@dataclass(frozen=True)
class SourceImage:
    product_id: int
    source_image_id: int | None
    source_key: str
    source_url: str


def _dedupe(urls: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(url for url in urls if url))


def source_images(product: Product) -> list[SourceImage]:
    gallery = sorted(product.images, key=lambda item: (item.sort_order, item.id))
    if gallery:
        return [
            SourceImage(product.id, image.id, f"gallery:{image.id}", image.image_url)
            for image in gallery
            if image.is_active and image.image_url
        ]
    attrs = product.attributes_json if isinstance(product.attributes_json, dict) else {}
    legacy = attrs.get("image_urls")
    urls = _dedupe(legacy if isinstance(legacy, list) else [legacy] if isinstance(legacy, str) else [product.image_url])
    return [
        SourceImage(product.id, None, f"legacy:{hashlib.sha256(url.encode()).hexdigest()[:32]}", url)
        for url in urls
    ]


async def collect_sources(limit: int | None) -> list[SourceImage]:
    async with AsyncSessionLocal() as session:
        products = list(
            (
                await session.execute(
                    select(Product).options(selectinload(Product.images)).order_by(Product.id)
                )
            ).scalars()
        )
    sources = [source for product in products for source in source_images(product)]
    return sources[:limit] if limit is not None else sources


async def _materialize_source(source: SourceImage) -> tuple[Path, str, tempfile.TemporaryDirectory[str] | None]:
    prefix = settings.upload_url_prefix.rstrip("/") + "/"
    if source.source_url.startswith(prefix):
        candidate = Path(settings.upload_dir) / source.source_url.removeprefix(prefix)
        if not candidate.is_file():
            raise FileNotFoundError(f"Original upload is missing: {candidate}")
        return candidate, _sha256_file(candidate), None
    if not source.source_url.startswith(("http://", "https://")):
        raise ValueError(f"Unsupported original image URL: {source.source_url}")
    temp_dir = tempfile.TemporaryDirectory(prefix="product-source-")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(source.source_url)
        response.raise_for_status()
    suffix = _source_suffix(source.source_url, response.headers.get("content-type"))
    path = Path(temp_dir.name) / f"source-image{suffix}"
    path.write_bytes(response.content)
    return path, _sha256_file(path), temp_dir


def _source_suffix(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        (content_type or "").split(";", 1)[0].lower(), ".jpg"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_output(content: bytes) -> None:
    with Image.open(BytesIO(content)) as image:
        width, height = image.size
        image.verify()
    if width <= 0 or height <= 0:
        raise ValueError("Composer output has invalid dimensions")


async def _existing_valid_variant(session, source: SourceImage, fingerprint: str) -> ProductImageVariant | None:
    variant = (
        await session.execute(
            select(ProductImageVariant).where(
                ProductImageVariant.product_id == source.product_id,
                ProductImageVariant.source_key == source.source_key,
                ProductImageVariant.source_fingerprint == fingerprint,
                ProductImageVariant.preset == PRESET,
                ProductImageVariant.recipe_version == RECIPE_VERSION,
            )
        )
    ).scalar_one_or_none()
    if variant and variant.status == "succeeded" and variant.output_upload_id:
        upload = await session.get(Upload, variant.output_upload_id)
        if upload and upload.file_path and Path(upload.file_path).is_file():
            return variant
    return None


async def process_source(source: SourceImage, args: argparse.Namespace, counts: Counter[str]) -> None:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        source_path, fingerprint, temporary = await _materialize_source(source)
        async with AsyncSessionLocal() as session:
            if await _existing_valid_variant(session, source, fingerprint):
                counts["skipped"] += 1
                return
            if args.dry_run:
                counts["queued"] += 1
                return
            variant = (
                await session.execute(
                    select(ProductImageVariant).where(
                        ProductImageVariant.product_id == source.product_id,
                        ProductImageVariant.source_key == source.source_key,
                        ProductImageVariant.source_fingerprint == fingerprint,
                        ProductImageVariant.preset == PRESET,
                        ProductImageVariant.recipe_version == RECIPE_VERSION,
                    )
                )
            ).scalar_one_or_none()
            if variant is None:
                variant = ProductImageVariant(
                    product_id=source.product_id,
                    source_image_id=source.source_image_id,
                    source_key=source.source_key,
                    source_url=source.source_url,
                    source_fingerprint=fingerprint,
                    preset=PRESET,
                    recipe_version=RECIPE_VERSION,
                )
                session.add(variant)
            elif variant.status == "failed" and not args.retry_failed:
                counts["skipped"] += 1
                return
            variant.status = "processing"
            variant.error_detail = None
            variant.attempt_count = (variant.attempt_count or 0) + 1
            await session.commit()
            await session.refresh(variant)

        composer = StudioProductComposerClient(
            args.composer_url, timeout_seconds=args.request_timeout, poll_seconds=args.poll_seconds, max_wait_seconds=args.max_wait_seconds
        )
        rendered = await composer.render_studio_light(
            source_path,
            name=f"soulcuts-product-{source.product_id}-{source.source_key[-12:]}",
            quality=args.removal_quality,
            resolution=args.resolution,
            output_format=args.output_format,
            output_quality=args.output_quality,
        )
        _validate_output(rendered.image)
        async with AsyncSessionLocal() as session:
            variant = (
                await session.execute(
                    select(ProductImageVariant).where(
                        ProductImageVariant.product_id == source.product_id,
                        ProductImageVariant.source_key == source.source_key,
                        ProductImageVariant.source_fingerprint == fingerprint,
                        ProductImageVariant.preset == PRESET,
                        ProductImageVariant.recipe_version == RECIPE_VERSION,
                    )
                )
            ).scalar_one()
            upload_file = UploadFile(
                filename=f"studio-light.{args.output_format}",
                file=BytesIO(rendered.image),
                headers={"content-type": f"image/{'jpeg' if args.output_format == 'jpg' else args.output_format}"},
            )
            upload_data = await save_image_upload(
                upload_file,
                folder=f"products/{source.product_id}/processed/studio-light/{source.source_key}-{fingerprint[:12]}",
                allowed_formats=PRODUCT_IMAGE_FORMATS,
            )
            upload = Upload(**upload_data)
            session.add(upload)
            await session.flush()
            await session.execute(
                update(ProductImageVariant)
                .where(
                    ProductImageVariant.product_id == source.product_id,
                    ProductImageVariant.source_key == source.source_key,
                    ProductImageVariant.preset == PRESET,
                    ProductImageVariant.recipe_version == RECIPE_VERSION,
                    ProductImageVariant.id != variant.id,
                )
                .values(is_preferred=False)
            )
            variant.output_upload_id = upload.id
            variant.output_url = upload.file_url
            variant.processor_version = rendered.processor_version
            variant.processing_config = {
                "background_preset": PRESET,
                "removal_quality": args.removal_quality,
                "resolution": args.resolution,
                "output_format": args.output_format,
                "output_quality": args.output_quality,
                **rendered.audit,
            }
            variant.status = "succeeded"
            variant.is_preferred = True
            await session.commit()
        counts["processed"] += 1
    except (ComposerError, FileNotFoundError, HTTPException, ValueError, httpx.HTTPError, OSError) as exc:
        counts["failed"] += 1
        if not args.dry_run:
            async with AsyncSessionLocal() as session:
                variant = (
                    await session.execute(
                        select(ProductImageVariant).where(
                            ProductImageVariant.product_id == source.product_id,
                            ProductImageVariant.source_key == source.source_key,
                            ProductImageVariant.preset == PRESET,
                            ProductImageVariant.recipe_version == RECIPE_VERSION,
                        ).order_by(ProductImageVariant.created_at.desc())
                    )
                ).scalars().first()
                if variant:
                    variant.status = "failed"
                    variant.error_detail = str(exc)[:4000]
                    variant.is_preferred = False
                    await session.commit()
    finally:
        if temporary is not None:
            temporary.cleanup()


async def run(args: argparse.Namespace) -> dict[str, int]:
    sources = await collect_sources(args.limit)
    counts: Counter[str] = Counter(discovered=len(sources))
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(source: SourceImage) -> None:
        async with semaphore:
            await process_source(source, args, counts)

    for start in range(0, len(sources), args.batch_size):
        await asyncio.gather(*(bounded(source) for source in sources[start : start + args.batch_size]))
    return {key: counts[key] for key in ("discovered", "queued", "processed", "skipped", "failed")}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Discover work only; do not create variants or call Composer")
    parser.add_argument("--limit", type=int, default=None, help="Maximum source images to inspect")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--composer-url", default="http://127.0.0.1:8765")
    parser.add_argument("--removal-quality", choices=("fast", "high", "ultra"), default="high")
    parser.add_argument("--resolution", choices=("4:5", "1:1", "9:12"), default="4:5")
    parser.add_argument("--output-format", choices=("webp", "png", "jpg"), default="webp")
    parser.add_argument("--output-quality", type=int, default=90)
    parser.add_argument("--request-timeout", type=float, default=60)
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--max-wait-seconds", type=float, default=900)
    return parser.parse_args()


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.batch_size < 1 or parsed.concurrency < 1 or not 1 <= parsed.output_quality <= 100:
        raise SystemExit("batch-size and concurrency must be positive; output-quality must be 1..100")
    print(json.dumps(asyncio.run(run(parsed)), ensure_ascii=False))
