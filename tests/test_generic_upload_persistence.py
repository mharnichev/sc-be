"""Generic upload persistence boundaries with synthetic images and a fake DB.

No application lifespan, database connection, or provider is used by these tests.
"""
from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy.exc import DataError, IntegrityError
from starlette.datastructures import Headers

from app.api.v1.routes import uploads as uploads_routes
from app.core.config import settings
from app.models.upload import Upload


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_url_prefix", "/media")
    monkeypatch.setattr(settings, "max_upload_size_bytes", 5 * 1024 * 1024)
    (tmp_path / "existing-file.txt").write_bytes(b"synthetic existing file")
    return tmp_path


def image_bytes(image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 3), color=(40, 80, 120)).save(output, format=image_format)
    return output.getvalue()


def upload_file(content: bytes, content_type: str | None = "image/png") -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename="Client Photo.jpg",
        headers=Headers({"content-type": content_type} if content_type else {}),
    )


class PersistenceSession:
    def __init__(
        self,
        *,
        failure_phase: str | None = None,
        error: Exception | None = None,
        rollback_error: Exception | None = None,
        committed_before_error: bool = False,
    ):
        self.failure_phase = failure_phase
        self.error = error
        self.rollback_error = rollback_error
        self.committed_before_error = committed_before_error
        self.events: list[str] = []
        self.upload: Upload | None = None
        self.flushed = False
        self.committed = False

    def fail_if(self, phase: str) -> None:
        if self.failure_phase == phase:
            assert self.error is not None
            raise self.error

    def add(self, upload: Upload) -> None:
        self.events.append("add")
        self.upload = upload
        self.fail_if("add")

    async def flush(self) -> None:
        self.events.append("flush")
        self.fail_if("flush")
        self.flushed = True

    async def commit(self) -> None:
        self.events.append("commit")
        # SQLAlchemy commit flushes pending rows even when a caller did not
        # explicitly flush first, as the original generic repository does.
        if not self.flushed:
            await self.flush()
        if self.failure_phase == "commit":
            self.committed = self.committed_before_error
        self.fail_if("commit")
        self.committed = True

    async def rollback(self) -> None:
        self.events.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error

    async def refresh(self, upload: Upload) -> None:
        self.events.append("refresh")
        self.fail_if("refresh")
        upload.id = 41
        upload.created_at = datetime(2026, 9, 6, 9, tzinfo=UTC)


def assert_existing_file_preserved(upload_root: Path) -> None:
    assert (upload_root / "existing-file.txt").read_bytes() == b"synthetic existing file"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("phase", "error"),
    [
        pytest.param("add", RuntimeError("add rejected"), id="add-failure"),
        pytest.param("flush", RuntimeError("flush rejected"), id="flush-failure"),
        pytest.param("commit", IntegrityError("INSERT uploads", {}, RuntimeError("constraint rejected")), id="commit-integrity-rejection"),
        pytest.param("commit", DataError("INSERT uploads", {}, RuntimeError("data rejected")), id="commit-data-rejection"),
    ],
)
async def test_definite_database_failure_cleans_new_file_after_successful_rollback(
    upload_root: Path, phase: str, error: Exception
) -> None:
    session = PersistenceSession(failure_phase=phase, error=error)

    with pytest.raises(type(error)) as caught:
        await uploads_routes.upload_file(file=upload_file(image_bytes()), session=session, _=object())

    assert caught.value is error
    assert session.events.count("rollback") == 1
    assert session.committed is False
    assert "refresh" not in session.events
    assert session.upload is not None
    assert not Path(session.upload.file_path).exists()
    assert_existing_file_preserved(upload_root)
    assert not list(upload_root.rglob("*.uploading"))


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["add", "flush", "commit"])
async def test_failed_rollback_keeps_file_and_preserves_original_error(upload_root: Path, phase: str) -> None:
    original = IntegrityError("INSERT uploads", {}, RuntimeError("write rejected"))
    session = PersistenceSession(
        failure_phase=phase,
        error=original,
        rollback_error=RuntimeError("rollback unavailable"),
    )

    with pytest.raises(IntegrityError) as caught:
        await uploads_routes.upload_file(file=upload_file(image_bytes()), session=session, _=object())

    assert caught.value is original
    assert session.events.count("rollback") == 1
    assert session.upload is not None
    assert Path(session.upload.file_path).is_file()
    assert_existing_file_preserved(upload_root)


@pytest.mark.anyio
@pytest.mark.parametrize("committed", [False, True], ids=["not-persisted", "persisted-before-connection-error"])
async def test_unknown_commit_failure_keeps_file_even_when_rollback_succeeds(upload_root: Path, committed: bool) -> None:
    original = RuntimeError("commit response lost")
    session = PersistenceSession(failure_phase="commit", error=original, committed_before_error=committed)

    with pytest.raises(RuntimeError) as caught:
        await uploads_routes.upload_file(file=upload_file(image_bytes()), session=session, _=object())

    assert caught.value is original
    assert session.committed is committed
    assert session.events.count("rollback") == 1
    assert "refresh" not in session.events
    assert session.upload is not None
    assert Path(session.upload.file_path).is_file()
    assert_existing_file_preserved(upload_root)


@pytest.mark.anyio
async def test_refresh_failure_after_successful_commit_keeps_file(upload_root: Path) -> None:
    original = RuntimeError("refresh unavailable")
    session = PersistenceSession(failure_phase="refresh", error=original)

    with pytest.raises(RuntimeError) as caught:
        await uploads_routes.upload_file(file=upload_file(image_bytes()), session=session, _=object())

    assert caught.value is original
    assert session.committed is True
    assert "rollback" not in session.events
    assert session.upload is not None
    assert Path(session.upload.file_path).is_file()
    assert_existing_file_preserved(upload_root)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("image_format", "declared_type", "canonical_type", "suffix"),
    [("PNG", "image/png", "image/png", ".png"), ("GIF", "image/gif", "image/gif", ".gif"), ("PNG", None, "image/png", ".png")],
    ids=["png", "gif", "png-without-declared-mime"],
)
async def test_successful_generic_upload_preserves_canonical_metadata(
    upload_root: Path, image_format: str, declared_type: str | None, canonical_type: str, suffix: str
) -> None:
    content = image_bytes(image_format)
    session = PersistenceSession()

    response = await uploads_routes.upload_file(file=upload_file(content, declared_type), session=session, _=object())

    path = Path(response.file_path)
    assert session.committed is True
    assert session.upload is not None
    assert response.id == session.upload.id == 41
    assert response.created_at == datetime(2026, 9, 6, 9, tzinfo=UTC)
    assert response.file_name == "Client Photo.jpg"
    assert response.content_type == canonical_type
    assert response.size == len(content)
    assert path.parent == upload_root / "images"
    assert path.suffix == suffix
    assert path.read_bytes() == content
    assert response.file_url == "/media/images/" + path.name
    assert "rollback" not in session.events
    assert_existing_file_preserved(upload_root)
    assert not list(upload_root.rglob("*.uploading"))


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["invalid-bytes", "mismatched-mime", "oversized"])
async def test_validation_failure_creates_no_file_or_database_write(
    upload_root: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    session = PersistenceSession()
    content = b"not an image" if scenario == "invalid-bytes" else image_bytes()
    content_type = "image/jpeg" if scenario == "mismatched-mime" else "image/png"
    if scenario == "oversized":
        monkeypatch.setattr(settings, "max_upload_size_bytes", 10)

    with pytest.raises(HTTPException) as caught:
        await uploads_routes.upload_file(file=upload_file(content, content_type), session=session, _=object())

    assert caught.value.status_code == (413 if scenario == "oversized" else 400)
    assert session.events == []
    assert session.upload is None
    assert [path for path in upload_root.rglob("*") if path.is_file()] == [upload_root / "existing-file.txt"]
    assert_existing_file_preserved(upload_root)
