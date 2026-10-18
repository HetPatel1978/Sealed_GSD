from fastapi.testclient import TestClient

from app.main import app


def local_client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def test_health():
    r = local_client().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["fo"] == "not_configured"
