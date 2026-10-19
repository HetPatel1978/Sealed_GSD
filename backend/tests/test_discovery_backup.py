"""Claude backup for dealer discovery: runs only with permission, after Fo's research is unusable or thin."""

import json
import threading

import httpx
import pytest

from sealed.discovery.claude import ClaudeResearch, SCHEMA, factory_from_env, to_report
from sealed.discovery.models import DiscoveryError, Report
from sealed.discovery.sources import Page
from sealed.discovery.worker import Discovery
from sealed.fo.adapter import Store as FoStore, _TURN_LOCK
from tests.test_discovery import BODY, Client, Web, candidate, request, settle


def dealer(name, site):
    return candidate(name=name, website=f"https://{site}/", email=f"sales@{site}", matching_listing_url=None,
                     sources=[f"https://{site}/contact"])


def body(name, site):
    return BODY.replace("Harbor Honda", name).replace("sales@harborhonda.com", f"sales@{site}")


SITES = {"harborhonda.com": "Harbor Honda", "lakesidehonda.com": "Lakeside Honda", "bayhonda.com": "Bay Honda"}


class Sites(Web):
    def fetch(self, url):
        host = url.split("/")[2].removeprefix("www.")
        return Page(url, body(SITES[host], host))


class Claude:
    def __init__(self, dealers=None, error=None):
        self.dealers, self.error, self.calls, self.lock_free = dealers or [], error, [], []

    def __call__(self):           # the factory
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def research(self, req):
        self.calls.append(req)
        # Another thread must be able to take the Fo lock while Claude works.
        def probe():
            ok = _TURN_LOCK.acquire(blocking=False)
            self.lock_free.append(ok)
            if ok:
                _TURN_LOCK.release()
        t = threading.Thread(target=probe)
        t.start(); t.join()
        if self.error:
            raise self.error
        return Report.model_validate({"dealers": self.dealers})


def fo_replies(client, dealers=None, status="complete"):
    def reply(cid, wait_seconds=20):
        client.replies.append(cid)
        if status != "complete":
            return {"status": status, "conversationId": cid}
        return {"status": "complete", "conversationId": cid, "response": json.dumps({"dealers": dealers})}
    client.reply = reply


THREE = [dealer("Harbor Honda", "harborhonda.com"), dealer("Lakeside Honda", "lakesidehonda.com"),
         dealer("Bay Honda", "bayhonda.com")]


@pytest.fixture
def setup(tmp_path):
    client, claude = Client(), Claude(THREE[1:])
    client.asks, client.replies = [], []
    service = Discovery(tmp_path, account="buyer@example.com", mode="live", fo_state=tmp_path / "fo.json",
                        client_factory=lambda: client, web_factory=Sites, claude_factory=claude)
    yield service, client, claude
    service.close()


def test_fo_failure_runs_claude_when_allowed(setup):
    service, client, claude = setup
    fo_replies(client, status="failed")
    result = settle(service, service.submit(request(), allow_claude=True)["id"])
    assert result["status"] == "complete"
    assert [d["found_by"] for d in result["dealers"]] == ["claude", "claude"]
    assert result["search"] | {} == {"primary": "fo", "fallback_allowed": True, "fallback_used": True,
        "fallback_reason": "fo_research_failed", "fallback_error": None, "fo_status": "failed"}
    assert claude.lock_free == [True]
    assert not FoStore(service.fo_state).data["active"]


def test_no_permission_means_no_backup(setup):
    service, client, claude = setup
    fo_replies(client, status="failed")
    result = settle(service, service.submit(request())["id"])
    assert result["status"] == "failed" and claude.calls == []
    assert result["search"]["fallback_used"] is False


def test_thin_fo_result_is_topped_up_and_deduped(setup):
    service, client, claude = setup
    fo_replies(client, [THREE[0]])
    claude.dealers = [dealer("Harbor Honda", "www.harborhonda.com")] + THREE[1:]
    result = settle(service, service.submit(request(), allow_claude=True)["id"])
    assert result["status"] == "complete"
    by = {d["name"]: d["found_by"] for d in result["dealers"]}
    assert by == {"Harbor Honda": "fo", "Lakeside Honda": "claude", "Bay Honda": "claude"}
    assert result["dropped"] == 1                       # Claude's repeat of Harbor Honda
    assert result["search"]["fallback_reason"] == "too_few"
    ids = [d["source"]["candidate_id"] for d in result["dealers"]]
    assert ids == ["candidate_1", "candidate_2", "candidate_3"]


def test_enough_fo_dealers_skip_the_backup(setup):
    service, client, claude = setup
    fo_replies(client, THREE)
    result = settle(service, service.submit(request(), allow_claude=True)["id"])
    assert len(result["dealers"]) == 3 and claude.calls == []
    assert result["search"]["fallback_used"] is False


def test_busy_fo_account_runs_backup_and_keeps_the_reservation(setup):
    service, client, claude = setup
    with _TURN_LOCK:
        FoStore(service.fo_state).set_status("uncertain-send", "pending", active=True)
        id_ = service.submit(request(), allow_claude=True)["id"]
    result = settle(service, id_)
    assert result["status"] == "complete" and len(claude.calls) == 1 and not client.asks
    assert result["error"] == "fo_account_requires_review"
    assert result["search"]["fo_status"] == "needs_review"
    assert FoStore(service.fo_state).data["active"] == "uncertain-send"


def test_claude_failure_restores_fo_outcome(setup):
    service, client, claude = setup
    fo_replies(client, status="failed")
    claude.error = DiscoveryError("claude_rate_limited")
    result = settle(service, service.submit(request(), allow_claude=True)["id"])
    assert result["status"] == "failed" and result["error"] == "fo_research_failed"
    assert result["search"]["fallback_error"] == "claude_rate_limited"


def test_backup_output_goes_through_the_same_checks(setup):
    service, client, claude = setup
    fo_replies(client, status="failed")
    claude.dealers = [dealer("Invented Honda", "harborhonda.com"), dealer("Lakeside Honda", "lakesidehonda.com")]
    result = settle(service, service.submit(request(), allow_claude=True)["id"])
    assert [d["name"] for d in result["dealers"]] == ["Lakeside Honda"] and result["dropped"] == 1


def test_restart_in_backup_phase_reruns_claude_only(setup):
    service, client, claude = setup
    service.stop.set(); service.wake.set(); service.thread.join(1)
    id_, _ = service.store.create(request(), allow_claude=True)
    service.store.update(id_, status="researching", phase="claude", error="fo_research_failed",
                         fallback=json.dumps({"reason": "fo_research_failed", "fo_status": "failed", "error": None}))
    service.stop.clear()
    service._run(id_)
    assert service.store.view(id_)["status"] == "complete" and not client.asks and len(claude.calls) == 1


def test_permission_given_later_runs_backup_without_reasking_fo(setup):
    service, client, claude = setup
    fo_replies(client, status="failed")
    first = service.submit(request())
    assert settle(service, first["id"])["status"] == "failed"
    again = service.submit(request(), allow_claude=True)
    assert again["cached"] and again["id"] == first["id"]
    result = settle(service, first["id"])
    assert result["status"] == "complete" and len(client.asks) == 1 and len(claude.calls) == 1


def test_simulation_never_constructs_claude(tmp_path):
    def forbidden():
        pytest.fail("Claude used in simulation")
    service = Discovery(tmp_path, account="buyer@example.com", mode="sim", fo_state=tmp_path / "fo.json",
                        client_factory=forbidden, web_factory=forbidden, claude_factory=forbidden)
    try:
        result = settle(service, service.submit(request(), allow_claude=True)["id"])
        assert result["status"] == "complete" and result["search"]["fallback_allowed"] is False
    finally:
        service.close()


# ---- the Claude client ------------------------------------------------------------------------------------
def messages_api(*responses, seen=None):
    queue = list(responses)

    def handler(req):
        if seen is not None:
            seen.append(req)
        status, data = queue.pop(0)
        return httpx.Response(status, json=data)
    return httpx.Client(transport=httpx.MockTransport(handler))


def final(dealers):
    return {"stop_reason": "end_turn", "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "honda dealers"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
        {"type": "text", "text": json.dumps({"dealers": dealers})}]}


def test_claude_request_is_public_only_and_resumes_paused_turns():
    seen = []
    paused = {"stop_reason": "pause_turn", "content": [{"type": "server_tool_use", "id": "s1", "name": "web_fetch",
                                                        "input": {"url": "https://harborhonda.com/"}}]}
    no_sources = {**THREE[1], "sources": []}
    six = [dealer(f"Honda {i}", f"honda{i}.com") for i in range(6)]
    with ClaudeResearch("test-key", client=messages_api((200, paused), (200, final([no_sources] + six)), seen=seen)) as c:
        report = c.research(request())
    assert len(report.dealers) == 5 and report.dealers[0].name == "Honda 0"      # bad one dropped, capped at 5
    first, second = (json.loads(r.content) for r in seen)
    assert [t["type"] for t in first["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    assert first["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert second["messages"][1] == {"role": "assistant", "content": paused["content"]}
    text = json.dumps(first)
    assert "173.27" not in text and "ceiling" not in text.lower() and "target" not in text.lower()
    assert seen[0].headers["x-api-key"] == "test-key" and seen[0].url == "https://api.anthropic.com/v1/messages"


@pytest.mark.parametrize("status,data,code", [
    (429, {}, "claude_rate_limited"), (401, {}, "claude_auth_failed"), (500, {}, "claude_service_error"),
    (200, {"stop_reason": "refusal", "content": []}, "claude_declined"),
    (200, {"stop_reason": "max_tokens", "content": []}, "claude_incomplete_response"),
    (200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]}, "claude_invalid_response"),
])
def test_claude_errors_are_fixed_codes(status, data, code):
    with pytest.raises(DiscoveryError, match=code):
        ClaudeResearch("k", client=messages_api((status, data))).research(request())


def test_missing_key_and_env_switches(monkeypatch):
    with pytest.raises(DiscoveryError, match="claude_not_configured"):
        ClaudeResearch("")
    assert factory_from_env("sim") is None
    monkeypatch.setenv("SEALED_DISCOVERY_FALLBACK", "off")
    assert factory_from_env("live") is None
    monkeypatch.delenv("SEALED_DISCOVERY_FALLBACK")
    monkeypatch.setenv("ANTHROPIC_DISCOVERY_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert factory_from_env("live")().model == "claude-sonnet-5-5"
    assert to_report('{"dealers": []}').dealers == []


def test_route_passes_claude_permission_separately(tmp_path, monkeypatch):
    from app.main import create_app
    from tests.test_jobs_api import local
    seen = []
    monkeypatch.setattr(Discovery, "submit", lambda self, public, allow_claude=False:
                        seen.append((public.model_dump(), allow_claude)) or {"id": "search_x", "cached": False})
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        body = {"item": "2026 Honda Civic Sport", "spec": {"details": "Civic Sport sedan"}, "zip": "19104"}
        assert client.post("/dealers/discover", json={**body, "allow_claude_research": True}).status_code == 409
        r = client.post("/dealers/discover", json={**body, "allow_fo_research": True, "allow_claude_research": True})
        assert r.status_code == 202
    public, allowed = seen[0]
    assert allowed is True and "allow_claude_research" not in public and "allow_fo_research" not in public


@pytest.mark.parametrize("fo", ["thin", "failed"])
def test_pollers_never_see_a_final_status_before_the_backup(setup, fo):
    service, client, claude = setup
    fo_replies(client, [THREE[0]]) if fo == "thin" else fo_replies(client, status="failed")
    statuses, original = [], service.store.update
    def update(id_, **fields):
        if "status" in fields:
            statuses.append(fields["status"])
        return original(id_, **fields)
    service.store.update = update
    result = settle(service, service.submit(request(), allow_claude=True)["id"])
    assert result["status"] == "complete" and claude.calls
    assert statuses.count("complete") == 1 and statuses[-1] == "complete" and "failed" not in statuses


def test_failed_backup_on_busy_account_still_allows_hets_retry(setup):
    service, client, claude = setup
    claude.error = DiscoveryError("claude_rate_limited")
    with _TURN_LOCK:
        FoStore(service.fo_state).set_status("uncertain-send", "pending", active=True)
        id_ = service.submit(request(), allow_claude=True)["id"]
    blocked = settle(service, id_)
    assert blocked["status"] == "needs_review" and blocked["error"] == "fo_account_requires_review"
    assert service.store.get(id_)["phase"] == "new" and not client.asks
    with _TURN_LOCK:
        FoStore(service.fo_state).set_status("uncertain-send", "reviewed", active=False)
    fo_replies(client, THREE)
    claude.error = None
    again = service.submit(request(), allow_claude=True)
    assert again["id"] == id_
    result = settle(service, id_)
    assert result["status"] == "complete" and len(client.asks) == 1
    assert {d["found_by"] for d in result["dealers"]} == {"fo"} and result["search"]["fallback_used"] is False
