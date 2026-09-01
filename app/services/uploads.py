from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from app.core.config import settings

IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}
DEFAULT_ALLOWED_FORMATS = frozenset(IMAGE_FORMATS)


def _safe_file_stem(file_name: str) -> str:
    stem = Path(file_name).stem.strip().lower()
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-")
    return stem or "upload"


async def save_image_upload(
    file: UploadFile,
    *,
    folder: str,
    allowed_formats: set[str] | frozenset[str] | None = None,
) -> dict:
    """Persist a validated image and return storage metadata.

    The file is streamed to a temporary path first.  Pillow then validates the
    actual bytes and determines the canonical MIME type and extension; the
    client supplied MIME type is only accepted when it agrees with that
    detected format.  Callers can narrow the formats for a particular use
    case (for example, product galleries do not accept GIF).
    """
    configured_formats = DEFAULT_ALLOWED_FORMATS if allowed_formats is None else allowed_formats
    allowed = {item.upper() for item in configured_formats}
    if not allowed:
        raise ValueError("At least one image format must be allowed")
    unsupported = allowed.difference(IMAGE_FORMATS)
    if unsupported:
        raise ValueError(f"Unsupported image formats: {', '.join(sorted(unsupported))}")

    client_content_type = (file.content_type or "").lower()
    allowed_mime_types = {IMAGE_FORMATS[item][0] for item in allowed}
    allowed_format_names = ", ".join(sorted(allowed))
    if client_content_type and client_content_type not in allowed_mime_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only {allowed_format_names} images are supported",
        )

    safe_name = _safe_file_stem(file.filename or "upload")
    relative_folder = Path(folder)
    if relative_folder.is_absolute() or ".." in relative_folder.parts:
        raise ValueError("Upload folder must be a relative path")

    upload_root = Path(settings.upload_dir)
    destination_dir = upload_root / relative_folder
    destination_dir.mkdir(parents=True, exist_ok=True)
    unique_name = f"{safe_name}-{uuid.uuid4().hex}"
    temporary_path = destination_dir / f".{unique_name}.uploading"
    destination = None

    size = 0
    try:
        with temporary_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_size_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Uploaded file is too large",
                    )
                output.write(chunk)

        try:
            with Image.open(temporary_path) as image:
                detected_format = (image.format or "").upper()
                image.verify()
        except (EOFError, OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is not a valid image",
            ) from exc

        if detected_format not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only {allowed_format_names} images are supported",
            )

        canonical_content_type, extension = IMAGE_FORMATS[detected_format]
        if client_content_type and client_content_type != canonical_content_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded MIME type does not match the image content",
            )

        stored_name = f"{unique_name}{extension}"
        relative_path = relative_folder / stored_name
        destination = upload_root / relative_path
        temporary_path.replace(destination)
        url_prefix = settings.upload_url_prefix.rstrip("/")
        file_url = f"{url_prefix}/{relative_path.as_posix()}"
        return {
            "file_name": file.filename or stored_name,
            "file_path": str(destination),
            "file_url": file_url,
            "content_type": canonical_content_type,
            "size": size,
        }
    except Exception:
        temporary_path.unlink(missing_ok=True)
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise


def delete_upload_file(file_path: str | None) -> None:
    if not file_path:
        return
    Path(file_path).unlink(missing_ok=True)
