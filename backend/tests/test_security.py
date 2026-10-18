import pytest
from fastapi.testclient import TestClient

from app.main import DASHBOARD_ORIGIN, app
from app.security import ConfigError, check_bind_host, check_origin


def client(host="127.0.0.1", base="http://127.0.0.1:8000") -> TestClient:
    return TestClient(app, base_url=base, client=(host, 50000))


def test_loopback_clients_are_served():
    assert client("127.0.0.1").get("/health").status_code == 200
    assert client("::1").get("/health").status_code == 200


def test_remote_clients_are_refused():
    r = client("203.0.113.7").get("/health")
    assert r.status_code == 403 and r.json() == {"error": "local_only"}


def test_non_local_host_header_is_refused():
    # A page on evil.example rebinding its DNS to 127.0.0.1 still sends Host: evil.example
    assert client(base="http://evil.example").get("/health").status_code == 400


def test_cors_allows_only_the_dashboard_origin():
    def preflight(origin):
        return client().options("/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})

    assert preflight(DASHBOARD_ORIGIN).headers.get("access-control-allow-origin") == DASHBOARD_ORIGIN
    assert "access-control-allow-origin" not in preflight("https://evil.example").headers
    simple = client().get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "example.com"])
def test_startup_check_rejects_non_loopback_bind(host):
    with pytest.raises(ConfigError):
        check_bind_host(host)


@pytest.mark.parametrize("origin", ["*", "http://localhost:3000,https://evil.example", "https://evil.example",
                                    "http://192.168.1.20:3000", "file://", "http://localhost:3000/app"])
def test_startup_check_rejects_unsafe_origins(origin):
    with pytest.raises(ConfigError):
        check_origin(origin)


def test_default_bind_and_origin_are_local(monkeypatch):
    from app.security import bind_host, dashboard_origin
    monkeypatch.delenv("SEALED_HOST", raising=False)
    monkeypatch.delenv("SEALED_DASHBOARD_ORIGIN", raising=False)
    monkeypatch.delenv("SEALED_CORS_ORIGINS", raising=False)
    assert bind_host() == "127.0.0.1" and dashboard_origin() == "http://localhost:3000"
