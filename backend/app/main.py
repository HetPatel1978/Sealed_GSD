"""Sealed backend. Only /health exists so far; see docs/api.md for the full contract.

Local only: see app/security.py (loopback clients, local Host header, one localhost dashboard origin).
Start it with `python -m app.run`, which binds to 127.0.0.1.
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.security import ALLOWED_HOST_HEADERS, LoopbackOnly, dashboard_origin
from sealed.env import load_env

load_env()

VERSION = "0.1.0"
DASHBOARD_ORIGIN = dashboard_origin()        # startup check: raises ConfigError if not one local origin

app = FastAPI(title="Sealed", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[DASHBOARD_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOST_HEADERS)
app.add_middleware(LoopbackOnly)


@app.get("/health")
def health() -> dict:
    # Integrations are reported honestly: nothing is wired yet.
    return {
        "status": "ok",
        "version": VERSION,
        "mode": os.getenv("SEALED_MODE", "sim"),
        "fo": "not_configured",
        "gmail": "not_configured",
    }
