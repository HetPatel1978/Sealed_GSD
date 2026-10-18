"""Local-only guards. The API has no login and GET /jobs/{id}/ceiling returns the ceiling, so:

1. The server binds to a loopback address only (checked at startup by `app.run`).
2. Requests from non-loopback clients are refused (403), even if someone starts uvicorn with --host 0.0.0.0.
3. The Host header must be a local name (blocks DNS-rebinding pages in the user's browser).
4. CORS allows exactly one dashboard origin, and it must be on localhost.
Any misconfiguration raises ConfigError at startup instead of running unsafely.
"""

import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}
DEFAULT_ORIGIN = "http://localhost:3000"


class ConfigError(RuntimeError):
    pass


def bind_host() -> str:
    host = os.getenv("SEALED_HOST", "127.0.0.1").strip()
    check_bind_host(host)
    return host


def check_bind_host(host: str) -> None:
    if host not in LOOPBACK_HOSTS and host != "localhost":
        raise ConfigError(f"SEALED_HOST must be a loopback address (127.0.0.1 or ::1), not {host!r}: "
                          "the API has no login and serves the ceiling to the dashboard.")


def dashboard_origin() -> str:
    raw = (os.getenv("SEALED_DASHBOARD_ORIGIN") or os.getenv("SEALED_CORS_ORIGINS") or DEFAULT_ORIGIN).strip()
    check_origin(raw)
    return raw.rstrip("/")


def check_origin(origin: str) -> None:
    if "," in origin or "*" in origin:
        raise ConfigError("Exactly one dashboard origin is allowed; no lists, no wildcards.")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or parts.hostname not in {"localhost", "127.0.0.1", "::1"} \
            or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError(f"Dashboard origin must be a local origin like {DEFAULT_ORIGIN}, not {origin!r}.")


class LoopbackOnly(BaseHTTPMiddleware):
    """Refuses any request that doesn't come from this machine."""

    async def dispatch(self, request: Request, call_next):
        client = request.client.host if request.client else None
        if client not in LOOPBACK_HOSTS:
            return JSONResponse({"error": "local_only"}, status_code=403)
        return await call_next(request)


ALLOWED_HOST_HEADERS = ["127.0.0.1", "localhost", "[::1]"]
