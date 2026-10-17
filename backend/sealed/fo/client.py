"""Thin synchronous wrapper around the Fo MCP tools (mcp SDK 2.x, streamable HTTP).

One MCP session lives on a background event loop thread; callers use plain blocking methods. The access
token is refreshed (without a browser) before any call when it is near expiry, and the session reconnects
with the new token. Results are the tools' `structured_content` dicts (Fo publishes output schemas).
"""

import asyncio
import threading
from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from sealed.fo.auth import FO_URL, FileTokenStorage, refresh_if_needed

CALL_TIMEOUT_S = 120


class FoTimeout(Exception):
    """The tool call itself timed out. The turn may still be running on Fo's side: poll, never resend."""


class FoClient(Protocol):
    def ask(self, message: str, conversation_id: str | None) -> dict: ...
    def reply(self, conversation_id: str | None, wait_seconds: int = 40) -> dict: ...
    def conversation(self, conversation_id: str) -> dict: ...
    def conversations(self) -> list[dict]: ...


class LiveFoClient:
    def __init__(self, storage: FileTokenStorage | None = None, url: str = FO_URL):
        self.storage, self.url = storage or FileTokenStorage(), url
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session: ClientSession | None = None
        self._stack = None
        self._token: str | None = None

    # -- public, blocking ---------------------------------------------------------------------------
    def ask(self, message: str, conversation_id: str | None) -> dict:
        args = {"message": message}
        args |= {"conversationId": conversation_id} if conversation_id else {"newConversation": True}
        return self._call("ask_fo", args)

    def reply(self, conversation_id: str | None, wait_seconds: int = 40) -> dict:
        args = {"waitSeconds": wait_seconds} | ({"conversationId": conversation_id} if conversation_id else {})
        return self._call("get_fo_reply", args)

    def conversation(self, conversation_id: str) -> dict:
        return self._call("get_fo_conversation", {"conversationId": conversation_id})

    def conversations(self) -> list[dict]:
        return self._call("list_fo_conversations", {}).get("conversations", [])

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop).result(30)
        self._loop.call_soon_threadsafe(self._loop.stop)

    # -- internals ------------------------------------------------------------------------------------
    def _call(self, tool: str, args: dict) -> dict:
        fut = asyncio.run_coroutine_threadsafe(self._acall(tool, args), self._loop)
        return fut.result(CALL_TIMEOUT_S + 30)

    async def _acall(self, tool: str, args: dict) -> dict:
        refresh_if_needed(self.storage)
        token = self.storage.load()["tokens"]["access_token"]
        if self._session is None or token != self._token:
            await self._disconnect()
            await self._connect(token)
        try:
            result = await self._session.call_tool(tool, args, read_timeout_seconds=CALL_TIMEOUT_S)
        except TimeoutError as e:
            raise FoTimeout(f"{tool} timed out after {CALL_TIMEOUT_S}s") from e
        if result.is_error:
            text = " ".join(getattr(b, "text", "") for b in result.content or [])
            return {"status": "failed", "response": text or "Fo returned an error."}
        return dict(result.structured_content or {})

    async def _connect(self, token: str) -> None:
        from contextlib import AsyncExitStack
        self._stack = AsyncExitStack()
        http_client = await self._stack.enter_async_context(
            create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}))
        read, write = await self._stack.enter_async_context(streamable_http_client(self.url, http_client=http_client))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        self._token = token

    async def _disconnect(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack, self._session, self._token = None, None, None
