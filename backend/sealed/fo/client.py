"""Thin synchronous wrapper around the Fo MCP tools (mcp SDK 2.x, streamable HTTP).

One MCP session lives on a background event loop thread; callers use plain blocking methods. The access
token is refreshed (without a browser) before any call when it is near expiry, and the session reconnects
with the new token. Results are the tools' `structured_content` dicts (Fo publishes output schemas).
"""

import asyncio
import threading
from concurrent.futures import Future
from typing import Protocol

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT

from sealed.fo.auth import FO_URL, FileTokenStorage, refresh_if_needed

CALL_TIMEOUT_S = 120
CLIENT_TIMEOUT_S = 180          # also bounds refresh, connection setup, and time waiting in the queue


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
        self._closed = False
        self._state_lock = threading.Lock()
        self._requests = asyncio.Queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session: ClientSession | None = None
        self._stack = None
        self._token: str | None = None
        # This task owns every MCP context, including initialization, refresh reconnects and shutdown.
        self._worker = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)

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
        with self._state_lock:
            if self._loop.is_closed():
                return
            if not self._closed:
                self._closed = True
                self._loop.call_soon_threadsafe(self._requests.put_nowait, None)
        try:
            self._worker.result(30)
        finally:
            if self._worker.done():
                with self._state_lock:
                    if not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self._loop.stop)
                        self._thread.join(30)
                        if not self._thread.is_alive():
                            self._loop.close()

    # -- internals ------------------------------------------------------------------------------------
    def _call(self, tool: str, args: dict) -> dict:
        fut = Future()
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Fo client is closed.")
            self._loop.call_soon_threadsafe(self._requests.put_nowait, (tool, args, fut))
        try:
            return fut.result(CLIENT_TIMEOUT_S)
        except TimeoutError as e:
            fut.cancel()  # Skip a request still waiting in the queue; an already running call is not resent.
            raise FoTimeout("Fo client timed out; the turn may still be running.") from e

    async def _serve(self) -> None:
        try:
            while (request := await self._requests.get()) is not None:
                tool, args, fut = request
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    # asyncio.timeout keeps execution in this owner task (wait_for would create another).
                    async with asyncio.timeout(CLIENT_TIMEOUT_S):
                        result = await self._acall(tool, args)
                except TimeoutError:
                    fut.set_exception(FoTimeout("Fo client timed out; the turn may still be running."))
                except Exception as e:
                    fut.set_exception(e)
                else:
                    fut.set_result(result)
        finally:
            await self._disconnect()

    async def _acall(self, tool: str, args: dict) -> dict:
        refresh_if_needed(self.storage)
        token = self.storage.load()["tokens"]["access_token"]
        if self._session is None or token != self._token:
            await self._disconnect()
            await self._connect(token)
        try:
            result = await self._session.call_tool(tool, args, read_timeout_seconds=CALL_TIMEOUT_S)
        except MCPError as e:
            if e.code != REQUEST_TIMEOUT:
                raise
            raise FoTimeout(f"{tool} timed out after {CALL_TIMEOUT_S}s") from e
        except TimeoutError as e:
            raise FoTimeout(f"{tool} timed out after {CALL_TIMEOUT_S}s") from e
        if result.is_error:
            text = " ".join(getattr(b, "text", "") for b in result.content or [])
            return {"status": "failed", "response": text or "Fo returned an error."}
        return dict(result.structured_content or {})

    async def _connect(self, token: str) -> None:
        from contextlib import AsyncExitStack
        stack = AsyncExitStack()
        try:
            http_client = await stack.enter_async_context(
                create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}))
            read, write = await stack.enter_async_context(streamable_http_client(self.url, http_client=http_client))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._session, self._token = stack, session, token

    async def _disconnect(self) -> None:
        try:
            if self._stack is not None:
                await self._stack.aclose()
        finally:
            self._stack, self._session, self._token = None, None, None
