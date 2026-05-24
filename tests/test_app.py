from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_openapi_is_available() -> None:
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200


def test_webp_media_is_served_with_image_content_type() -> None:
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    media_file = upload_dir / "test-content-type.webp"
    media_file.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

    try:
        client = TestClient(app)
        response = client.get(f"{settings.upload_url_prefix}/{media_file.name}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"
    finally:
        media_file.unlink(missing_ok=True)
