"""Exercise the real blocking client without network or credential files."""

import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT

from sealed.fo import client as module
from sealed.fo.client import FoTimeout, LiveFoClient


class Storage:
    def __init__(self):
        self.token = "fake-token-one"

    def load(self):
        return {"tokens": {"access_token": self.token}}


class InitializeFailure(Exception):
    pass


@pytest.fixture(autouse=True)
def isolate_fo_credentials(monkeypatch):
    monkeypatch.delenv("FO_API_KEY", raising=False)


@pytest.fixture
def fake_mcp(monkeypatch):
    events, calls, script = [], [], []
    storage = Storage()
    monkeypatch.setattr(module, "refresh_if_needed", lambda _: False)

    @asynccontextmanager
    async def http(**kwargs):
        owner = asyncio.current_task()
        events.append(("http_open", owner))
        storage.last_headers = kwargs["headers"]
        try:
            yield object()
        finally:
            assert asyncio.current_task() is owner
            events.append(("http_close", owner))

    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield None, None

    class Session:
        async def __aenter__(self):
            self.owner = asyncio.current_task()
            # An actual AnyIO task group reproduces the SDK's task-ownership requirement.
            self.group = anyio.create_task_group()
            await self.group.__aenter__()
            events.append(("session_open", self.owner))
            return self

        async def __aexit__(self, *exc):
            assert asyncio.current_task() is self.owner
            events.append(("session_close", self.owner))
            return await self.group.__aexit__(*exc)

        async def initialize(self):
            assert asyncio.current_task() is self.owner
            if script and isinstance(script[0], InitializeFailure):
                raise script.pop(0)

        async def call_tool(self, name, args, **kwargs):
            assert asyncio.current_task() is self.owner
            calls.append((name, args))
            item = script.pop(0) if script else {"status": "complete", "response": "Sent."}
            if isinstance(item, Exception):
                raise item
            return SimpleNamespace(is_error=False, structured_content=item)

    monkeypatch.setattr(module, "create_mcp_http_client", http)
    monkeypatch.setattr(module, "streamable_http_client", stream)
    monkeypatch.setattr(module, "ClientSession", lambda *args: Session())
    return storage, script, calls, events


def test_mcp_timeout_code_is_translated_to_fo_timeout_and_client_stays_usable(fake_mcp):
    storage, script, calls, events = fake_mcp
    script.append(MCPError(REQUEST_TIMEOUT, "SDK request timed out"))
    client = LiveFoClient(storage)
    try:
        with pytest.raises(FoTimeout):
            client.ask("test", None)
        assert client.reply("conversation-one")["status"] == "complete"
        assert [c[0] for c in calls] == ["ask_fo", "get_fo_reply"]
    finally:
        client.close()
    assert len({task for _, task in events}) == 1
    assert [event for event, _ in events][-2:] == ["session_close", "http_close"]


def test_token_rotation_reconnects_and_closes_in_the_original_owner_task(fake_mcp):
    storage, script, calls, events = fake_mcp
    client = LiveFoClient(storage)
    try:
        client.ask("first", None)
        storage.token = "fake-token-two"
        client.reply("conversation-one")
    finally:
        client.close()
    assert [event for event, _ in events] == ["http_open", "session_open", "session_close", "http_close"] * 2
    assert len({task for _, task in events}) == 1


def test_non_timeout_sdk_errors_propagate_without_being_misclassified(fake_mcp):
    storage, script, calls, events = fake_mcp
    script.append(MCPError(-32603, "server failure"))
    client = LiveFoClient(storage)
    try:
        with pytest.raises(MCPError) as exc:
            client.ask("test", None)
        assert exc.value.code == -32603
    finally:
        client.close()


def test_close_is_idempotent_and_closed_clients_reject_new_requests(fake_mcp):
    client = LiveFoClient(fake_mcp[0])
    client.ask("test", None)
    client.close()
    client.close()
    assert not client._thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        client.ask("never send", None)


def test_failed_initialization_closes_partial_contexts_before_reconnecting(fake_mcp):
    storage, script, calls, events = fake_mcp
    script.append(InitializeFailure("failed setup"))
    client = LiveFoClient(storage)
    try:
        with pytest.raises(InitializeFailure):
            client.ask("setup failure", None)
        assert client.reply("conversation-one")["status"] == "complete"
    finally:
        client.close()
    assert [event for event, _ in events] == ["http_open", "session_open", "session_close", "http_close"] * 2


def test_parallel_callers_share_one_session_owner(fake_mcp):
    client = LiveFoClient(fake_mcp[0])
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: client.ask(f"request {i}", None), range(6)))
        assert all(r["status"] == "complete" for r in results)
    finally:
        client.close()
    assert len(fake_mcp[2]) == 6 and len({task for _, task in fake_mcp[3]}) == 1


def test_real_sdk_no_response_timeout_uses_polling_exception_type(monkeypatch):
    """Use the installed SDK's dispatcher and in-memory channels, not a made-up TimeoutError."""
    monkeypatch.setattr(module, "CALL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(module, "refresh_if_needed", lambda _: False)

    @asynccontextmanager
    async def http(**kwargs):
        yield None

    @asynccontextmanager
    async def stream(*args, **kwargs):
        inbound_send, inbound_read = anyio.create_memory_object_stream(16)
        outbound_write, outbound_receive = anyio.create_memory_object_stream(16)
        async with inbound_send, outbound_receive:
            yield inbound_read, outbound_write

    async def initialize(self):
        pass

    monkeypatch.setattr(module, "create_mcp_http_client", http)
    monkeypatch.setattr(module, "streamable_http_client", stream)
    monkeypatch.setattr(module.ClientSession, "initialize", initialize)
    client = LiveFoClient(Storage())
    try:
        with pytest.raises(FoTimeout) as exc:
            client.ask("in-memory test", None)
        assert isinstance(exc.value.__cause__, MCPError)
        assert exc.value.__cause__.code == REQUEST_TIMEOUT
    finally:
        client.close()


def test_environment_key_uses_bearer_header_without_oauth_reads_or_refresh(fake_mcp, monkeypatch):
    storage, script, calls, events = fake_mcp
    monkeypatch.setenv("FO_API_KEY", "wak_test_key_only")
    monkeypatch.setattr(storage, "load", lambda: pytest.fail("OAuth state read in API-key mode"))
    monkeypatch.setattr(module, "refresh_if_needed", lambda _: pytest.fail("OAuth refresh in API-key mode"))
    script.extend([{"conversations": []}, {"conversations": []}])
    client = LiveFoClient(storage)
    try:
        assert client.auth_mode == "api_key"
        assert client.conversations() == [] and client.conversations() == []
        assert storage.last_headers == {"Authorization": "Bearer wak_test_key_only"}
        assert [name for name, args in calls] == ["list_fo_conversations", "list_fo_conversations"]
    finally:
        client.close()
    assert [name for name, _ in events].count("http_open") == 1


def test_api_key_does_not_construct_oauth_storage(fake_mcp, monkeypatch):
    monkeypatch.setattr(module, "FileTokenStorage", lambda: pytest.fail("OAuth storage constructed"))
    client = LiveFoClient(api_key="wak_explicit_test_key")
    try:
        assert client.storage is None
        client.conversations()
        assert fake_mcp[0].last_headers["Authorization"] == "Bearer wak_explicit_test_key"
    finally:
        client.close()


def test_api_key_auth_error_never_falls_back_to_oauth(fake_mcp, monkeypatch):
    storage, script, calls, events = fake_mcp
    monkeypatch.setattr(storage, "load", lambda: pytest.fail("Silently fell back to OAuth"))
    monkeypatch.setattr(module, "refresh_if_needed", lambda _: pytest.fail("Silently refreshed OAuth"))
    script.append(MCPError(-32603, "Unauthorized"))
    client = LiveFoClient(storage, api_key="wak_rejected_test_key")
    try:
        with pytest.raises(MCPError):
            client.conversations()
        assert len(calls) == 1
        assert storage.last_headers["Authorization"] == "Bearer wak_rejected_test_key"
    finally:
        client.close()


def test_explicit_key_overrides_environment_and_blank_explicit_key_selects_oauth(fake_mcp, monkeypatch):
    storage = fake_mcp[0]
    monkeypatch.setenv("FO_API_KEY", "wak_environment_test_key")
    client = LiveFoClient(storage, api_key="wak_explicit_test_key")
    try:
        client.conversations()
        assert storage.last_headers["Authorization"] == "Bearer wak_explicit_test_key"
    finally:
        client.close()
    client = LiveFoClient(storage, api_key="")
    try:
        client.conversations()
        assert client.auth_mode == "oauth"
        assert storage.last_headers["Authorization"] == "Bearer fake-token-one"
    finally:
        client.close()


@pytest.mark.parametrize("key", ["wak_invalid\r\nInjected: value", "wak_invalid key", "wak_nonascii_\u00e9"])
def test_invalid_key_is_rejected_without_echoing_its_value(key):
    with pytest.raises(ValueError) as error:
        LiveFoClient(api_key=key)
    assert key not in str(error.value)
