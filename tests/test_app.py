from fastapi.testclient import TestClient

from app.main import app


def test_openapi_is_available() -> None:
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200
