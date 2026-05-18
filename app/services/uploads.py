from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _safe_file_stem(file_name: str) -> str:
    stem = Path(file_name).stem.strip().lower()
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-")
    return stem or "upload"


async def save_image_upload(file: UploadFile, *, folder: str) -> dict:
    if file.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG, PNG, WEBP and GIF images are supported",
        )

    extension = IMAGE_EXTENSIONS[file.content_type]
    safe_name = _safe_file_stem(file.filename or "upload")
    stored_name = f"{safe_name}-{uuid.uuid4().hex}{extension}"
    relative_path = Path(folder) / stored_name
    upload_root = Path(settings.upload_dir)
    destination = upload_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_size_bytes:
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Uploaded file is too large",
                )
            output.write(chunk)

    url_prefix = settings.upload_url_prefix.rstrip("/")
    file_url = f"{url_prefix}/{relative_path.as_posix()}"
    return {
        "file_name": file.filename or stored_name,
        "file_path": str(destination),
        "file_url": file_url,
        "content_type": file.content_type,
        "size": size,
    }


def delete_upload_file(file_path: str | None) -> None:
    if not file_path:
        return
    Path(file_path).unlink(missing_ok=True)
