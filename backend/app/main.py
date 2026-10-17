"""Sealed backend. Only /health exists so far; see docs/api.md for the full contract."""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

VERSION = "0.1.0"

app = FastAPI(title="Sealed", version=VERSION)

origins = [o for o in os.getenv("SEALED_CORS_ORIGINS", "http://localhost:3000").split(",") if o]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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
