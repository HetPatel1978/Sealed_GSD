"""Sealed backend. /health and POST /dealers/discover so far; see docs/api.md for the full contract.

Local only: see app/security.py (loopback clients, local Host header, one localhost dashboard origin).
Start it with `python -m app.run`, which binds to 127.0.0.1.
"""

import json
import os
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.security import ALLOWED_HOST_HEADERS, LoopbackOnly, dashboard_origin
from sealed.discovery.claude_finder import ClaudeFinder
from sealed.discovery.dealers import discover, order_from_env
from sealed.discovery.fetch import Fetcher
from sealed.discovery.fo_finder import FoFinder
from sealed.discovery.models import DiscoveryUnavailable
from sealed.env import REPO_ROOT, load_env
from sealed.fo.auth import token_path

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


class Spec(BaseModel):
    quantity: int = 1
    details: str = ""


class DiscoverRequest(BaseModel):
    item: str = Field(min_length=1, max_length=200)
    spec: Spec = Spec()
    zip: str = Field(pattern=r"^\d{5}$")
    radius_miles: Literal[20] = 20          # 10 and 50 come after the freeze (docs/plan.md)


_fo_client = None


def _fo():
    """The live Fo client, or None if Fo isn't connected (no OAuth token file)."""
    global _fo_client
    if _fo_client is None and token_path().exists():
        from sealed.fo.client import LiveFoClient
        _fo_client = LiveFoClient()
    return _fo_client


def _finders() -> list:
    make = {"fo": lambda: FoFinder(_fo()), "claude": ClaudeFinder}
    return [make[n]() for n in order_from_env() if n in make]


@app.post("/dealers/discover")
def dealers_discover(req: DiscoverRequest):
    # Only the car, the ZIP and the radius go to a search. Contacts no one.
    if os.getenv("SEALED_MODE", "sim") != "live":
        return json.loads((REPO_ROOT / "mock" / "discovery.json").read_text())
    car = req.spec.details or req.item
    try:
        result = discover(car, req.zip, _finders(), Fetcher(), radius_miles=req.radius_miles)
    except DiscoveryUnavailable:
        return JSONResponse({"error": "discovery_unavailable"}, status_code=503)
    return {**result, "mode": "live"}
