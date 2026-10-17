"""Spike 1a: call Fo MCP directly from Python with OAuth (mcp SDK 2.x, streamable HTTP).

Run from the repo root with the backend venv:
    backend/.venv/Scripts/python -u spikes/fo_oauth.py

First run opens a browser for Wajo sign-in; tokens are saved to .fo_oauth.json (gitignored)
and reused on later runs. Sends ONE harmless message to Fo in a new conversation.
Never put a budget, ceiling or max price in any message here.
"""

import asyncio
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

FO_URL = os.getenv("FO_MCP_URL") or "https://mcp.api.wajo.ai/fo"
TOKEN_PATH = Path(os.getenv("FO_OAUTH_TOKEN_PATH") or Path(__file__).resolve().parent.parent / ".fo_oauth.json")
CALLBACK_PORT = 3030
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
TEST_MESSAGE = "Hi Fo, this is a connection test from my own code. No action needed, just reply OK."
POLL_LIMIT_S = 600


class FileTokenStorage(TokenStorage):
    """Stores OAuth tokens and the dynamically registered client in one gitignored JSON file."""

    def _load(self) -> dict:
        return json.loads(TOKEN_PATH.read_text()) if TOKEN_PATH.exists() else {}

    def _save(self, data: dict) -> None:
        TOKEN_PATH.write_text(json.dumps(data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        t = self._load().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        c = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(data)


async def redirect_handler(auth_url: str) -> None:
    print(f"\n[oauth] Opening browser for Wajo sign-in. If it doesn't open, visit:\n{auth_url}\n", flush=True)
    webbrowser.open(auth_url)


async def callback_handler() -> AuthorizationCodeResult:
    """Serves http://localhost:3030/callback until the OAuth redirect arrives (5 min limit)."""
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if "code" in q or "error" in q:
                result.update({k: v[0] for k, v in q.items()})
                done.set()
                msg = b"Sealed spike: sign-in received. You can close this tab."
            else:
                msg = b"Waiting for OAuth redirect..."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg)

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", CALLBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not await asyncio.to_thread(done.wait, 300):
            raise TimeoutError("No OAuth redirect within 5 minutes")
    finally:
        server.shutdown()
    if "error" in result:
        raise RuntimeError(f"OAuth error: {result.get('error')}: {result.get('error_description')}")
    print("[oauth] Authorization code received.", flush=True)
    return AuthorizationCodeResult(code=result["code"], state=result.get("state"), iss=result.get("iss"))


def dump(label: str, result) -> None:
    print(f"\n===== {label} =====", flush=True)
    print(result.model_dump_json(indent=2, exclude_none=True), flush=True)


def payload(result) -> dict:
    """Prefer the structured result (Fo now publishes output schemas); fall back to the text block."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"_raw_text": block.text}
    return {}


async def timed(session: ClientSession, name: str, args: dict):
    t0 = time.perf_counter()
    result = await session.call_tool(name, args, read_timeout_seconds=120)
    dt = time.perf_counter() - t0
    print(f"\n[timing] {name} took {dt:.1f}s", flush=True)
    return result, dt


async def main() -> None:
    print(f"[info] Fo MCP: {FO_URL}\n[info] Token file: {TOKEN_PATH} (exists: {TOKEN_PATH.exists()})", flush=True)
    auth = OAuthClientProvider(
        server_url=FO_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Sealed (hackathon spike)",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    t_connect = time.perf_counter()
    async with create_mcp_http_client(auth=auth) as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print(f"\n[timing] connect + initialize took {time.perf_counter() - t_connect:.1f}s", flush=True)
                dump("initialize", init)

                tools = await session.list_tools()
                print("\n===== tools =====", flush=True)
                for tool in tools.tools:
                    print(f"\n--- {tool.name} ---")
                    print(json.dumps(tool.input_schema, indent=2))
                    if tool.output_schema:
                        print("output_schema:", json.dumps(tool.output_schema, indent=2))
                    if tool.annotations:
                        print("annotations:", tool.annotations.model_dump_json(exclude_none=True))

                result, ask_s = await timed(session, "ask_fo", {"message": TEST_MESSAGE, "newConversation": True})
                dump("ask_fo raw result", result)
                p = payload(result)
                conv = p.get("conversationId")
                status = p.get("status")
                total = ask_s

                t_poll = time.perf_counter()
                n = 0
                while status == "working" and time.perf_counter() - t_poll < POLL_LIMIT_S:
                    n += 1
                    args = {"waitSeconds": 40, **({"conversationId": conv} if conv else {})}
                    result, dt = await timed(session, "get_fo_reply", args)
                    total += dt
                    dump(f"get_fo_reply #{n} raw result", result)
                    p = payload(result)
                    status = p.get("status")

                print(f"\n[summary] final status: {status!r}; polls: {n}; "
                      f"Fo answered in {total:.1f}s total (ask_fo {ask_s:.1f}s + polling {total - ask_s:.1f}s)",
                      flush=True)


if __name__ == "__main__":
    asyncio.run(main())
