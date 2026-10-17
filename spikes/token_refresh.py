"""Checks Fo token refresh without a browser, then proves the new token works with a read-only call.

    backend/.venv/Scripts/python -u spikes/token_refresh.py [--force]

Refreshes only if the token is expired or within 5 minutes of expiry, unless --force.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sealed.env import load_env  # noqa: E402

load_env()

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402

from sealed.fo.auth import FO_URL, FileTokenStorage, refresh, refresh_if_needed, seconds_left  # noqa: E402


async def read_only_check(storage: FileTokenStorage) -> int:
    token = storage.load()["tokens"]["access_token"]
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_fo_conversations", {})
                return len((result.structured_content or {}).get("conversations", []))


def main() -> None:
    storage = FileTokenStorage()
    before = storage.load()["tokens"]
    left = seconds_left(storage)
    print(f"[token] seconds left before: {left:.0f}" if left is not None else "[token] expiry unknown")
    did = refresh(storage) is not None if "--force" in sys.argv else refresh_if_needed(storage)
    after = storage.load()["tokens"]
    print(f"[token] refreshed: {did}")
    if did:
        print(f"[token] access token changed: {before['access_token'] != after['access_token']}; "
              f"refresh token rotated: {before.get('refresh_token') != after.get('refresh_token')}; "
              f"new expires_in: {after.get('expires_in')}s; seconds left now: {seconds_left(storage):.0f}")
    n = asyncio.run(read_only_check(storage))
    print(f"[check] list_fo_conversations with the current token (no OAuth provider, no browser): {n} conversations")


if __name__ == "__main__":
    main()
