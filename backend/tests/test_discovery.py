import json
import threading
import time

import pytest

from sealed.discovery.models import DiscoveryError, Request, parse_report, prompt
from sealed.discovery.sources import Page, PublicWeb, public_addresses, safe_url, validate_candidates
from sealed.discovery.worker import Discovery
from sealed.fo.adapter import Store as FoStore, _TURN_LOCK


def request():
    return Request(item="2026 Honda Civic Sport", spec={"details": "2026 Honda Civic Sport sedan Crystal Black Pearl automatic"}, zip="19104")


def candidate(**changes):
    return {"name": "Harbor Honda", "website": "https://harborhonda.com/", "address": "123 Main Street, Test City, PA 19104",
        "email": "sales@harborhonda.com", "phone": "(215) 555-0101", "matching_listing_url": "https://harborhonda.com/new/unit",
        "sources": ["https://harborhonda.com/contact", "https://harborhonda.com/new/unit"], **changes}


BODY = """<p>Harbor Honda</p><p>123 Main Street, Test City, PA 19104</p>
<p>sales@harborhonda.com 215-555-0101</p><p>2026 Honda Civic Sport sedan Crystal Black Pearl automatic</p>
<p>VIN 2HGFE2F57TH641108</p><p>In Transit</p>"""


class Web:
    def fetch(self, url):
        return Page(url, BODY)
    def zip_point(self, code):
        return (39.95, -75.2) if code == "19104" else (45, -80)


def settle(discovery, id_):
    for _ in range(200):
        result = discovery.store.view(id_)
        if result["status"] in {"complete", "failed", "needs_review"}:
            return result
        time.sleep(.01)
    pytest.fail("Discovery did not settle")


class Client:
    asks = []
    replies = []
    def conversations(self):
        return []
    def ask(self, message, conversation_id):
        self.asks.append((message, conversation_id))
        return {"status": "working", "conversationId": "test-conversation"}
    def reply(self, cid, wait_seconds=20):
        self.replies.append(cid)
        return {"status": "complete", "conversationId": cid, "response": json.dumps({"dealers": [candidate()]})}
    def close(self):
        pass


@pytest.fixture
def discovery(tmp_path):
    client = Client()
    client.asks, client.replies = [], []
    service = Discovery(tmp_path, account="buyer@example.com", mode="live", fo_state=tmp_path / "fo.json",
                        client_factory=lambda: client, web_factory=Web)
    yield service, client
    service.close()


def test_live_discovery_asks_once_polls_validates_and_reuses(discovery):
    service, client = discovery
    result = service.submit(request())
    completed = settle(service, result["id"])
    assert completed["status"] == "complete"
    dealer = completed["dealers"][0]
    assert dealer["email"] == "sales@harborhonda.com"
    assert dealer["inventory_status"] == "listed_in_transit" and dealer["in_stock"] is None
    assert dealer["distance_miles"] == 0
    assert len(client.asks) == 1 and client.replies == ["test-conversation"]
    reused = service.submit(request())
    assert reused["cached"] and reused["id"] == result["id"]
    assert len(client.asks) == 1
    assert not FoStore(service.fo_state).data["active"]


def test_output_format_followup_is_distinct_and_bounded(discovery):
    service, client = discovery
    original = client.ask
    def ask(message, cid):
        if cid is None:
            client.asks.append((message, cid))
            return {"status": "complete", "conversationId": "test-conversation", "response": "Attached document."}
        return original(message, cid)
    client.ask = ask
    result = settle(service, service.submit(request())["id"])
    assert result["status"] == "complete"
    assert len(client.asks) == 2 and client.asks[1][1] == "test-conversation"
    assert "ALREADY" in client.asks[1][0]


def test_progress_reads_remain_available_during_slow_fo_call(discovery):
    service, client = discovery
    entered, release = threading.Event(), threading.Event()
    original = client.ask
    def slow(message, cid):
        entered.set()
        release.wait(2)
        return original(message, cid)
    client.ask = slow
    result = service.submit(request())
    assert entered.wait(1)
    try:
        assert service.store.view(result["id"])["status"] == "researching"
        assert service.submit(request())["id"] == result["id"]
        assert not _TURN_LOCK.acquire(blocking=False)
    finally:
        release.set()
    assert settle(service, result["id"])["status"] == "complete"


def test_existing_account_pause_is_preserved(discovery):
    service, client = discovery
    with _TURN_LOCK:
        state = FoStore(service.fo_state)
        state.set_status("uncertain-send", "pending", active=True)
        result = service.submit(request())
    assert settle(service, result["id"])["status"] == "needs_review"
    assert not client.asks
    assert FoStore(service.fo_state).data["active"] == "uncertain-send"


def test_blocked_unattempted_search_can_be_submitted_after_account_review(discovery):
    service, client = discovery
    with _TURN_LOCK:
        state = FoStore(service.fo_state)
        state.set_status("uncertain-send", "unclear", active=True)
        result = service.submit(request())
    id_ = result["id"]
    assert settle(service, id_)["error"] == "fo_account_requires_review"
    assert not client.asks
    assert service.submit(request())["status"] == "needs_review"
    with _TURN_LOCK:
        # Simulate a completed, explicitly reviewed account release.
        state = FoStore(service.fo_state)
        state.set_status("uncertain-send", "unclear", active=False, review={"confirmed": True})
    retried = service.submit(request())
    assert retried["id"] == id_ and retried["cached"]
    assert settle(service, id_)["status"] == "complete"
    assert len(client.asks) == 1
    assert service.submit(request())["id"] == id_ and len(client.asks) == 1


@pytest.mark.parametrize("phase,cid,error", [("ask_pending", None, "research_requires_review"),
                                           ("ask_pending", "known-id", "research_requires_review"),
                                           ("new", "known-id", "fo_account_requires_review"),
                                           ("inline_pending", "known-id", "invalid_report")])
def test_resubmission_does_not_retry_an_attempted_or_uncertain_search(discovery, phase, cid, error):
    service, client = discovery
    service.stop.set(); service.wake.set(); service.thread.join(1)
    id_, _ = service.store.create(request())
    service.store.update(id_, status="needs_review", phase=phase, conversation_id=cid, error=error)
    assert service.submit(request())["id"] == id_
    assert service.store.view(id_)["status"] == "needs_review"
    assert not client.asks


def test_uncertain_attempt_without_conversation_is_never_reasked(discovery):
    service, client = discovery
    service.stop.set()
    service.wake.set()
    service.thread.join(1)
    id_, _ = service.store.create(request())
    service.store.update(id_, status="researching", phase="ask_pending")
    service.stop.clear()
    service._run(id_)
    assert service.store.view(id_)["status"] == "needs_review" and not client.asks


def test_restart_known_pending_conversation_polls_without_ask(discovery):
    service, client = discovery
    service.stop.set(); service.wake.set(); service.thread.join(1)
    id_, _ = service.store.create(request())
    service.store.update(id_, status="researching", phase="ask_pending", conversation_id="test-conversation")
    service.stop.clear()
    service._run(id_)
    assert service.store.view(id_)["status"] == "complete"
    assert not client.asks and client.replies == ["test-conversation"]


def test_fabricated_contacts_are_removed_and_stale_listing_unknown():
    item = candidate(email="invented@harborhonda.com")
    class Redirected(Web):
        def fetch(self, url):
            return Page("https://harborhonda.com/inventory", BODY)
    result = validate_candidates(request(), parse_report(json.dumps({"dealers": [item]})), Redirected())
    assert result["dealers"][0]["email"] is None
    assert result["dealers"][0]["phone"] == "(215) 555-0101"
    assert result["dealers"][0]["inventory_status"] == "unknown"


def test_drops_out_of_radius_duplicate_and_ungrounded_dealers():
    items = [candidate(), candidate(), candidate(name="Unpublished Honda", website="https://anotherhonda.com/"),
             candidate(address="123 Main Street, Test City, PA 99999", website="https://farhonda.com/")]
    class Far(Web):
        def fetch(self, url):
            return Page(url, BODY + " 123 Main Street, Test City, PA 99999")
    result = validate_candidates(request(), parse_report(json.dumps({"dealers": items})), Far())
    assert len(result["dealers"]) == 1 and result["dropped"] == 3


@pytest.mark.parametrize("url", ["http://example.com", "https://localhost", "https://127.0.0.1/", "https://10.0.0.1/",
    "https://[::1]/", "https://user:pass@example.com/", "https://example.com:444/", "https://foo.local/",
    "https://example.com/?api_key=secret", "https://example.com\\@127.0.0.1/", "file:///etc/passwd"])
def test_rejects_unsafe_source_urls(url):
    with pytest.raises(DiscoveryError):
        safe_url(url)


def test_mixed_public_private_dns_answers_are_rejected(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(None, None, None, None, (address, 443)) for address in ("8.8.8.8", "127.0.0.1")])
    with pytest.raises(DiscoveryError):
        public_addresses("example.com")


def test_request_excludes_private_terms_and_strict_report_rejects_extra_fields():
    with pytest.raises(ValueError):
        Request.model_validate({**request().model_dump(), "ceiling": 173.27})
    with pytest.raises(DiscoveryError):
        parse_report('{"dealers":[],"budget":173.27}')
    assert len(parse_report('```json\n{"dealers":[]}\n```').dealers) == 0
    assert "173.27" not in prompt(request())


def test_synthetic_discovery_never_constructs_live_clients(tmp_path):
    def forbidden():
        pytest.fail("live integration used in simulation")
    service = Discovery(tmp_path, account="buyer@example.com", mode="sim", fo_state=tmp_path / "fo.json", client_factory=forbidden, web_factory=forbidden)
    try:
        report = settle(service, service.submit(request())["id"])
        assert report["status"] == "complete" and len(report["dealers"]) == 3
        assert report["mode"] == "sim"
    finally:
        service.close()


def test_transport_pins_public_ip_and_preserves_tls_hostname(monkeypatch):
    from sealed.discovery.sources import PinnedHTTPS
    calls = []
    stream = object()
    monkeypatch.setattr("sealed.discovery.sources.public_addresses", lambda host: ["8.8.8.8"])
    monkeypatch.setattr("socket.create_connection", lambda address, timeout: calls.append(address) or stream)
    class TLS:
        def wrap_socket(self, sock, server_hostname):
            assert sock is stream and server_hostname == "harborhonda.com"
            return "verified-socket"
    connection = PinnedHTTPS("harborhonda.com", timeout=8)
    connection._context = TLS()
    connection.connect()
    assert calls == [("8.8.8.8", 443)] and connection.sock == "verified-socket"


@pytest.mark.parametrize("location", ["https://127.0.0.1/private", "https://another-site.com/", "http://harborhonda.com/"])
def test_redirect_cannot_escape_https_official_site(monkeypatch, location):
    calls = []
    class Connection:
        def __init__(self, host, **kwargs):
            calls.append(host)
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return self
        status = 302
        def getheader(self, name, default=None):
            return location if name == "Location" else default
        def close(self):
            pass
    monkeypatch.setattr("sealed.discovery.sources.PinnedHTTPS", Connection)
    with pytest.raises(DiscoveryError):
        PublicWeb().fetch("https://harborhonda.com/contact")
    assert calls == ["harborhonda.com"]
