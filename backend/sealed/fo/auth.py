"""Fo MCP OAuth: file token storage with absolute expiry, and refresh without a browser.

Why: the mcp SDK (2.3) forgets token expiry when it loads tokens from storage, sends the expired token,
gets 401 and then starts a full browser sign-in instead of refreshing. So we track `expires_at`
ourselves and refresh before connecting.
"""

import json
import os
import time
from pathlib import Path

import httpx

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from sealed.env import REPO_ROOT

FO_URL = os.getenv("FO_MCP_URL") or "https://mcp.api.wajo.ai/fo"
REFRESH_MARGIN_S = 300


def token_path() -> Path:
    return Path(os.getenv("FO_OAUTH_TOKEN_PATH") or REPO_ROOT / ".fo_oauth.json")


class FileTokenStorage(TokenStorage):
    """Tokens + registered client in one gitignored JSON file. Records saved_at / expires_at (epoch s)."""

    def __init__(self, path: Path | None = None):
        self.path = path or token_path()

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        t = self.load().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.store_tokens(tokens.model_dump(mode="json", exclude_none=True))

    def store_tokens(self, tokens: dict) -> None:
        data = self.load()
        now = time.time()
        data["tokens"] = tokens
        data["saved_at"] = now
        data["expires_at"] = now + tokens["expires_in"] if tokens.get("expires_in") else None
        self.save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        c = self.load().get("client_info")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self.load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self.save(data)

    def expires_at(self) -> float | None:
        data = self.load()
        if data.get("expires_at"):
            return data["expires_at"]
        # Files written before we tracked expiry: fall back to file mtime + expires_in.
        exp_in = data.get("tokens", {}).get("expires_in")
        return self.path.stat().st_mtime + exp_in if exp_in and self.path.exists() else None


def seconds_left(storage: FileTokenStorage) -> float | None:
    exp = storage.expires_at()
    return None if exp is None else exp - time.time()


def token_endpoint(issuer: str) -> str:
    meta = httpx.get(f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server", timeout=15).json()
    return meta["token_endpoint"]


def refresh(storage: FileTokenStorage) -> dict:
    """Exchanges the refresh token for a new access token. No browser. Returns the new token dict."""
    data = storage.load()
    tokens, client = data["tokens"], data["client_info"]
    resp = httpx.post(
        token_endpoint(client.get("issuer") or "https://mcp.api.wajo.ai"),
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
            "resource": FO_URL,
        },
        timeout=30,
    )
    resp.raise_for_status()
    new = resp.json()
    new.setdefault("refresh_token", tokens["refresh_token"])  # keep old one if not rotated
    storage.store_tokens(new)
    return new


def refresh_if_needed(storage: FileTokenStorage, margin_s: float = REFRESH_MARGIN_S) -> bool:
    """Refreshes when the token is expired or within `margin_s` of expiry. Returns True if refreshed."""
    left = seconds_left(storage)
    if left is not None and left > margin_s:
        return False
    if not storage.load().get("tokens", {}).get("refresh_token"):
        return False
    refresh(storage)
    return True
