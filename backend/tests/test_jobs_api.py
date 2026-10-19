import json
import sqlite3
import threading
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import DASHBOARD_ORIGIN, create_app
from sealed.private import vault


def local(api):
    return TestClient(api, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


@pytest.fixture
def client(tmp_path):
    with local(create_app(data_dir=tmp_path, mode="sim", background=False)) as client:
        yield client


def payload(**overrides):
    return {"item": "Sample item", "spec": {"quantity": 1, "details": "Exact sample specification"},
            "deadline": (date.today() + timedelta(days=30)).isoformat(), "target": 90, "ceiling": 173.27,
            "vendors": [{"name": "Sample seller", "email": "seller@example.org", "mode": "sim"}], **overrides}


def create(client):
    response = client.post("/jobs", json=payload())
    assert response.status_code == 201, response.text
    return response.json()


def proposal(client, id_):
    assert client.post(f"/jobs/{id_}/start", json={}).status_code == 200
    result = client.post("/sim/advance", json={"job_id": id_, "steps": -1})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "AWAITING_APPROVAL", client.get(f"/jobs/{id_}/events?follow=false").text
    return client.get(f"/jobs/{id_}").json()["proposal"]


def echo(p):
    return {k: p[k] for k in ("proposal_id", "vendor_id", "price_total", "currency", "delivery_date")}


def test_full_api_negotiation_requires_approval_and_written_confirmation(client):
    id_ = create(client)["id"]
    p = proposal(client, id_)
    assert p["price_total"] == 90
    stream = client.get(f"/jobs/{id_}/events?follow=false").text
    assert '"kind": "booking"' not in stream
    assert client.get(f"/jobs/{id_}/receipt").status_code == 409
    approved = client.post(f"/jobs/{id_}/approve", json=echo(p))
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "BOOKING"
    assert client.get(f"/jobs/{id_}/receipt").status_code == 409
    advanced = client.post("/sim/advance", json={"job_id": id_, "steps": -1})
    assert advanced.json()["state"] == "COMPLETED", advanced.text
    receipt = client.get(f"/jobs/{id_}/receipt").json()
    assert receipt["price_total"] == 90 and receipt["mode"] == "sim"
    assert receipt["evidence"] and receipt["verification"] == "VERIFIED"


def test_ceiling_only_returned_by_private_endpoint_not_public_state_or_database(client, tmp_path):
    job = create(client)
    id_ = job["id"]
    assert "ceiling" not in job and job["ceiling_set"] is True
    private = client.get(f"/jobs/{id_}/ceiling")
    assert private.json()["ceiling"] == 173.27
    assert private.headers["cache-control"] == "no-store"
    proposal(client, id_)
    assert "173.27" not in client.get("/jobs").text
    assert "173.27" not in client.get(f"/jobs/{id_}/events?follow=false").text
    with sqlite3.connect(tmp_path / "engine.sqlite3") as db:
        assert "173.27" not in "\n".join(db.iterdump())


@pytest.mark.parametrize("change", [
    {"ceiling": 12.34567}, {"ceiling": "private-invalid-9988"}, {"ceiling": 90},
    {"target": -1}, {"max_rounds": 3}, {"spec": {"quantity": 6, "details": "test"}},
    {"vendors": []}, {"vendors": [{"name": "bad", "email": "victim@example.org\nBcc:bad@example.org"}]},
])
def test_invalid_commands_are_sanitized_without_echoing_private_input(client, change):
    response = client.post("/jobs", json=payload(**change))
    assert response.status_code == 422
    assert response.json() == {"error": "validation_error"}


def test_sse_replays_only_after_cursor_in_order(client):
    id_ = create(client)["id"]
    proposal(client, id_)
    response = client.get(f"/jobs/{id_}/events?follow=false", headers={"Last-Event-ID": "3"})
    assert response.headers["content-type"].startswith("text/event-stream")
    records = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert records and records[0]["id"] == 4
    assert [e["id"] for e in records] == list(range(4, records[-1]["id"] + 1))
    empty = client.get(f"/jobs/{id_}/events?follow=false&last_event_id={records[-1]['id']}")
    assert empty.text == ""
    assert client.get(f"/jobs/{id_}/events?follow=false", headers={"Last-Event-ID": "invalid"}).status_code == 422
    assert client.get(f"/jobs/{id_}/events?follow=false&last_event_id=999999").status_code == 422


def test_stale_approval_and_rejection_do_not_book(client):
    id_ = create(client)["id"]
    p = proposal(client, id_)
    response = client.post(f"/jobs/{id_}/approve", json={**echo(p), "price_total": 89})
    assert response.status_code == 409 and response.json()["error"] == "stale_proposal"
    assert response.json()["current_proposal"] == p
    rejected = client.post(f"/jobs/{id_}/reject", json={"proposal_id": p["proposal_id"], "reason": "my private limit is 173.27"})
    assert rejected.status_code == 200
    assert "173.27" not in client.get(f"/jobs/{id_}/events?follow=false").text
    assert client.post(f"/jobs/{id_}/approve", json=echo(p)).status_code == 409


def test_unstarted_job_remains_created_and_double_start_is_rejected(client):
    id_ = create(client)["id"]
    assert client.get(f"/jobs/{id_}").json()["state"] == "CREATED"
    assert client.post("/sim/advance", json={"job_id": id_}).status_code == 409
    assert client.post(f"/jobs/{id_}/start", json={}).status_code == 200
    assert client.post(f"/jobs/{id_}/start", json={}).status_code == 409


def test_cancel_is_durable_and_does_not_send(client):
    id_ = create(client)["id"]
    client.post(f"/jobs/{id_}/start", json={})
    cancelled = client.post(f"/jobs/{id_}/cancel", json={"reason": "private 173.27"})
    assert cancelled.json()["state"] == "CANCELLED"
    before = client.get(f"/jobs/{id_}/events?follow=false").text
    client.post("/sim/advance", json={"job_id": id_, "steps": -1})
    assert client.get(f"/jobs/{id_}/events?follow=false").text == before
    assert "173.27" not in before


def test_restart_retains_public_job_requires_private_restore_and_replays_once(tmp_path):
    with local(create_app(data_dir=tmp_path, mode="sim", background=False)) as client:
        id_ = create(client)["id"]
        p = proposal(client, id_)
        events = client.get(f"/jobs/{id_}/events?follow=false").text
    assert not vault.has(id_)
    with local(create_app(data_dir=tmp_path, mode="sim", background=False)) as client:
        assert client.get(f"/jobs/{id_}").json()["ceiling_set"] is False
        assert client.get(f"/jobs/{id_}/ceiling").json()["ceiling"] is None
        assert client.post(f"/jobs/{id_}/approve", json=echo(p)).status_code == 409
        assert client.post(f"/jobs/{id_}/reject", json={"proposal_id": p["proposal_id"]}).status_code == 409
        assert client.post(f"/jobs/{id_}/private-runtime", json={"ceiling": 173.27}).json() == {"restored": True}
        assert client.post(f"/jobs/{id_}/private-runtime", json={"ceiling": 199}).status_code == 409
        assert client.get(f"/jobs/{id_}/events?follow=false").text == events
        assert client.post(f"/jobs/{id_}/approve", json=echo(p)).status_code == 200
        assert client.post("/sim/advance", json={"job_id": id_, "steps": -1}).json()["state"] == "COMPLETED"


def test_foreign_origins_and_form_posts_cannot_mutate_or_read(client):
    for origin in ("https://evil.example", "null", "http://localhost:9999"):
        response = client.post("/jobs", json=payload(), headers={"Origin": origin})
        assert response.status_code == 403
        assert client.get("/jobs", headers={"Origin": origin}).status_code == 403
    assert client.post("/jobs", data={"ceiling": "173.27"}).status_code == 415
    assert client.post("/jobs", json=payload(), headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    # localhost dashboard -> 127.0.0.1 API is cross-site, but explicitly trusted.
    assert client.post("/jobs", json=payload(), headers={"Origin": DASHBOARD_ORIGIN, "Sec-Fetch-Site": "cross-site"}).status_code == 201


def test_unknown_job_and_discovery_are_honest(client):
    assert client.get("/jobs/missing").status_code == 404
    assert client.get("/jobs/missing/ceiling").status_code == 404
    assert client.post("/dealers/discover", json={}).json() == {"error": "validation_error"}


def test_multiple_dealers_and_currency_use_same_coordinator(client):
    response = client.post("/jobs", json=payload(currency="EUR", vendors=[
        {"name": "One", "email": "one@example.org"}, {"name": "Two", "email": "two@example.org"}]))
    id_ = response.json()["id"]
    p = proposal(client, id_)
    assert p["currency"] == "EUR"
    assert len(client.get(f"/jobs/{id_}").json()["vendors"]) == 2


def test_background_worker_processes_started_job_without_auto_approving(tmp_path):
    with local(create_app(data_dir=tmp_path, mode="sim", interval=.1)) as client:
        id_ = create(client)["id"]
        time.sleep(.2)
        assert client.get(f"/jobs/{id_}").json()["state"] == "CREATED"
        assert client.post(f"/jobs/{id_}/start", json={}).status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            view = client.get(f"/jobs/{id_}").json()
            if view["vendors"][0]["latest_quote"]:
                break
            time.sleep(.04)
        assert view["vendors"][0]["latest_quote"]["price_total"] == 99
        assert view["state"] == "RUNNING"
        assert '"kind": "booking"' not in client.get(f"/jobs/{id_}/events?follow=false").text


def test_live_start_requires_permissions_and_polls_before_dispatch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("GMAIL_ADDRESS", "buyer@example.com")

    class Reader:
        address = "buyer@example.com"
        def __init__(self, *args):
            calls.append("connect")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.jobs.GmailReader", Reader)
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        body = payload(vendors=[{"name": "Seller", "email": "seller@example.org", "phone": "+15555550101", "mode": "live"}])
        id_ = client.post("/jobs", json=body).json()["id"]
        assert calls == []
        assert client.post(f"/jobs/{id_}/start", json={}).json() == {"error": "live_permissions_required"}
        assert calls == []
        assert client.post(f"/jobs/{id_}/start", json={"allow_dispatch": True}).status_code == 409
        assert calls == []
        # Initial tick queues the RFQ; it cannot have contacted Fo yet.
        response = client.post(f"/jobs/{id_}/start", json={"allow_dispatch": True, "allow_model_requests": True})
        assert response.status_code == 200, response.text
        assert calls == ["connect"]
        assert response.json()["state"] == "RUNNING"
        assert '"message.sent"' not in client.get(f"/jobs/{id_}/events?follow=false").text
        assert client.post("/sim/advance", json={"job_id": id_}).status_code == 404
        assert client.post(f"/jobs/{id_}/allow-calls", json={"allow_calls": False}).status_code == 422
        assert client.post(f"/jobs/{id_}/allow-calls", json={"allow_calls": True}).status_code == 200
        assert calls == ["connect"]


def test_live_phone_only_start_needs_call_authorization(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "buyer@example.com")
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        response = client.post("/jobs", json=payload(vendors=[{"name": "Seller", "phone": "+15555550101", "mode": "live"}]))
        id_ = response.json()["id"]
        start = client.post(f"/jobs/{id_}/start", json={"allow_dispatch": True, "allow_model_requests": True})
        assert start.status_code == 409 and start.json()["error"] == "call_authorization_required"


def test_simulation_fails_closed_for_mixed_mode_vendors(client):
    response = client.post("/jobs", json=payload(vendors=[{"name": "Seller", "email": "seller@example.org", "mode": "live"}]))
    assert response.status_code == 422 and response.json()["error"] == "vendor_mode_mismatch"


def discover(client):
    public = {"item": "Sample item", "spec": {"quantity": 1, "details": "Exact sample specification"}, "zip": "19104"}
    response = client.post("/dealers/discover", json=public)
    assert response.status_code == 202, response.text
    id_ = response.json()["id"]
    for _ in range(100):
        result = client.get(f"/dealers/discover/{id_}")
        assert result.headers["cache-control"] == "no-store"
        if result.json()["status"] == "complete":
            return public, result.json()
        time.sleep(.01)
    pytest.fail("Discovery did not complete")


def selected(dealer):
    return {key: dealer[key] for key in ("name", "email", "phone", "source")}


def test_discovery_selection_creates_unstarted_job_and_completes_approval_flow(client, tmp_path):
    public, report = discover(client)
    assert len(report["dealers"]) == 3 and report["mode"] == "sim"
    repeat = client.post("/dealers/discover", json=public).json()
    assert repeat["id"] == report["id"] and repeat["cached"]
    job = client.post("/jobs", json=payload(**public, vendors=[selected(report["dealers"][0])]))
    assert job.status_code == 201, job.text
    assert job.json()["state"] == "CREATED"
    assert job.json()["vendors"][0]["name"] == report["dealers"][0]["name"]
    id_ = job.json()["id"]
    p = proposal(client, id_)
    assert client.post(f"/jobs/{id_}/approve", json=echo(p)).status_code == 200
    assert client.post("/sim/advance", json={"job_id": id_, "steps": -1}).json()["state"] == "COMPLETED"
    with sqlite3.connect(tmp_path / "discovery.sqlite3") as db:
        assert "173.27" not in "\n".join(db.iterdump())


@pytest.mark.parametrize("change", ["email", "source", "item", "zip", "expired"])
def test_discovered_selection_rejects_changed_contacts_requirements_and_expired_sources(client, change):
    public, report = discover(client)
    dealer = selected(report["dealers"][0])
    if change == "email":
        dealer["email"] = "changed@example.org"
    elif change == "source":
        dealer["source"]["urls"] = ["https://unrelated.example/"]
    elif change in {"item", "zip"}:
        public[change] = "Other item" if change == "item" else "19103"
    else:
        store = client.app.state.discovery.store
        with store.connection() as db:
            db.execute("UPDATE discovery_jobs SET updated=0 WHERE id=?", (report["id"],))
    response = client.post("/jobs", json=payload(**public, vendors=[dealer]))
    assert response.status_code == 422 and response.json()["error"] == "discovery_selection_changed"
    assert client.get("/jobs").json() == []


@pytest.mark.parametrize("extra", [{"ceiling": 173.27}, {"target": 90}, {"buyer": "Private buyer"}])
def test_discovery_public_input_rejects_private_or_negotiation_fields(client, extra):
    response = client.post("/dealers/discover", json={"item": "Car", "spec": {"details": "Car details"}, "zip": "19104", **extra})
    assert response.status_code == 422 and response.json() == {"error": "validation_error"}
    assert client.get("/dealers/discover/missing").status_code == 404


def test_live_discovery_requires_permission_before_any_provider_work(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "buyer@example.com")
    monkeypatch.setattr("sealed.discovery.worker.LiveFoClient", lambda: pytest.fail("Unauthorized Fo call"))
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        response = client.post("/dealers/discover", json={"item": "Car", "spec": {"details": "Car details"}, "zip": "19104"})
        assert response.status_code == 409 and response.json()["error"] == "discovery_permission_required"
        assert client.app.state.discovery.store.pending() == []


def test_slow_live_research_keeps_api_responsive_and_defers_negotiation_tick(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setenv("GMAIL_ADDRESS", "buyer@example.com")
    class Research:
        def conversations(self):
            return []
        def ask(self, message, cid):
            entered.set()
            release.wait(3)
            return {"status": "complete", "conversationId": "research", "response": '{"dealers":[]}'}
        def close(self):
            pass
    class Web:
        def zip_point(self, code):
            return 0, 0
    monkeypatch.setattr("sealed.discovery.worker.LiveFoClient", Research)
    monkeypatch.setattr("sealed.discovery.worker.PublicWeb", Web)
    monkeypatch.setattr("app.jobs.GmailReader", lambda *a: pytest.fail("Tick did not defer while Fo was researching"))
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        try:
            started = client.post("/dealers/discover", json={"item": "Car", "spec": {"details": "Car details"}, "zip": "19104", "allow_fo_research": True})
            assert entered.wait(1)
            before = time.monotonic()
            assert client.get(f"/dealers/discover/{started.json()['id']}").json()["status"] == "researching"
            job = client.post("/jobs", json=payload(vendors=[{"name": "Seller", "email": "seller@example.org", "mode": "live"}])).json()
            response = client.post(f"/jobs/{job['id']}/start", json={"allow_model_requests": True, "allow_dispatch": True})
            assert response.status_code == 200
            assert response.json()["worker_status"] == "fo_busy_research"
            assert client.get("/jobs").status_code == 200
            assert time.monotonic()-before < 1.5
        finally:
            release.set()


def test_api_tracks_pending_call_and_observes_it_without_private_restore_or_imap(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from sealed.service.sim_demo import Extractor, Mailbox
    from tests.test_call_lifecycle import Provider, report
    monkeypatch.setenv("GMAIL_ADDRESS", "buyer@example.com")
    reader = Mailbox()
    monkeypatch.setattr("app.jobs.GmailReader", lambda *a: nullcontext(reader))
    monkeypatch.setattr("app.jobs.QuoteParser", lambda *a: nullcontext(Extractor()))
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        body = payload(buyer="Hari", vendors=[{"name": "Call seller", "phone": "+1-215-555-0101", "mode": "live"}])
        job = client.post("/jobs", json=body).json()
        id_ = job["id"]
        response = client.post(f"/jobs/{id_}/start", json={"allow_model_requests": True, "allow_dispatch": True, "allow_calls": True})
        assert response.status_code == 200
        svc, executor = client.app.state.service, client.app.state.executor
        msg = executor.submit(lambda: next(iter(svc.engine(id_).outbound.values()))).result(3)
        provider = Provider(msg)
        provider.close = lambda: None
        monkeypatch.setattr("sealed.service.live.LiveFoClient", lambda: provider)
        executor.submit(svc.tick).result(3)
        current = client.get(f"/jobs/{id_}").json()
        assert current["state"] == "RUNNING" and current["worker_status"] == "call_in_progress"
        assert current["vendors"][0]["state"] == "CALL_IN_PROGRESS"
        events = client.get(f"/jobs/{id_}/events?follow=false").text
        assert '"type": "call.pending"' in events and '"type": "job.needs_human"' not in events
        vault.forget(id_)
        monkeypatch.setattr("app.jobs.GmailReader", lambda *a: pytest.fail("IMAP used for existing-call observation"))
        provider.turns = [{"role": "assistant", "text": report(msg).model_dump_json()}]
        executor.submit(svc.tick).result(3)
        current = client.get(f"/jobs/{id_}").json()
        assert current["state"] == "RUNNING" and current["ceiling_set"] is False
        assert current["vendors"][0]["latest_quote"] is None
        assert '"type": "call.completed"' in client.get(f"/jobs/{id_}/events?follow=false").text
        assert len(provider.asks) == 2
