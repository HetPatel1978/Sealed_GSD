"""Dashboard resolution of an unclear Fo send or call: no network, credentials or dealer outreach."""

import json
from contextlib import nullcontext

import pytest

from sealed.core.call_policy import expected_plan, request_id
from sealed.fo.adapter import Store
from sealed.fo.calls import CallTransport
from tests.test_call_lifecycle import Provider
from tests.test_jobs_api import local, payload
from app.main import create_app

UNCLEAR = "Plan prepared, nothing dialed. I'm holding here until your separate instruction arrives."


class Fo:
    """Answers each call plan from a script; records every ask so a repeated permission is visible."""

    def __init__(self, *plans):
        self.plans, self.asks, self.replies = list(plans), [], []

    def ask(self, text, cid):
        self.asks.append(text)
        plan = self.plans.pop(0)
        return {"status": "complete", "conversationId": "call-convo", "response": plan(text) if callable(plan) else plan}

    def reply(self, cid, wait_seconds=40):
        self.replies.append(cid)
        return {"status": "complete", "conversationId": cid, "response": UNCLEAR}

    def close(self):
        pass


def matching_plan(svc, id_):
    """A plan that matches whichever call message is currently queued."""
    def plan(text):
        eng = svc.engine(id_)
        msg = next(m for m in eng.outbound.values() if request_id(m) in text)
        return json.dumps(expected_plan(msg).model_dump())
    return plan


@pytest.fixture
def live(tmp_path, monkeypatch):
    from sealed.service.sim_demo import Extractor, Mailbox
    monkeypatch.setenv("GMAIL_ADDRESS", "buyer@example.com")
    reader = Mailbox()
    monkeypatch.setattr("app.jobs.GmailReader", lambda *a: nullcontext(reader))
    monkeypatch.setattr("app.jobs.QuoteParser", lambda *a: nullcontext(Extractor()))
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        body = payload(buyer="Hari", vendors=[{"name": "Call seller", "phone": "+1-215-555-0101", "mode": "live"}])
        id_ = client.post("/jobs", json=body).json()["id"]
        assert client.post(f"/jobs/{id_}/start", json={"allow_model_requests": True, "allow_dispatch": True,
                                                       "allow_calls": True}).status_code == 200
        yield client, id_, tmp_path / "fo.json"


def paused(client, id_, monkeypatch, fo):
    monkeypatch.setattr("sealed.service.live.LiveFoClient", lambda: fo)
    svc, executor = client.app.state.service, client.app.state.executor
    executor.submit(svc.tick).result(3)
    job = client.get(f"/jobs/{id_}").json()
    assert job["state"] == "NEEDS_HUMAN"
    return job


def tick(client):
    svc, executor = client.app.state.service, client.app.state.executor
    executor.submit(svc.tick).result(3)


def test_unclear_call_plan_offers_continue_and_retry_asks_once_with_a_new_message(live, monkeypatch):
    client, id_, state = live
    svc = client.app.state.service
    fo = Fo(UNCLEAR, matching_plan(svc, id_), "On it, calling now. I'll report back here after it ends.")
    job = paused(client, id_, monkeypatch, fo)
    review = job["review"]
    assert review == {"message_id": "m_01", "vendor_id": "v_01", "channel": "call", "kind": "rfq",
                      "reason_code": "send_unclear", "event_id": review["event_id"], "can_skip": True}
    assert Store(state).data["active"] == json.dumps([id_, job["run_id"], "m_01"], separators=(",", ":"))

    resolved = client.post(f"/jobs/{id_}/resolve", json={"message_id": "m_01", "outcome": "retry", "checked": True})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["state"] == "RUNNING" and resolved.json()["review"] is None
    fo_state = Store(state)
    first = fo_state.message(json.dumps([id_, job["run_id"], "m_01"], separators=(",", ":")))
    assert fo_state.data["active"] is None
    assert first["status"] == "unclear" and first["resolutions"][0]["outcome"] == "retry"

    tick(client)  # the retry is a fresh plan under m_02, then one dial permission
    assert len(fo.asks) == 3 and "m_01" not in fo.asks[1]
    current = client.get(f"/jobs/{id_}").json()
    assert current["worker_status"] == "call_in_progress"
    events = [json.loads(line[6:]) for line in client.get(f"/jobs/{id_}/events?follow=false").text.splitlines()
              if line.startswith("data: ")]
    resumed = [e for e in events if e["type"] == "job.resumed"]
    assert len(resumed) == 1 and resumed[0]["data"]["resolves_event_id"] == review["event_id"]
    # The same pause cannot be resolved twice.
    again = client.post(f"/jobs/{id_}/resolve", json={"message_id": "m_01", "outcome": "retry", "checked": True})
    assert again.status_code == 409 and again.json()["error"] == "review_not_available"


def test_confirmed_sent_records_without_resending_and_skip_only_resumes(live, monkeypatch):
    client, id_, state = live
    fo = Fo(UNCLEAR)
    paused(client, id_, monkeypatch, fo)
    assert client.post(f"/jobs/{id_}/resolve", json={"message_id": "m_01", "outcome": "sent", "checked": True}).status_code == 200
    events = client.get(f"/jobs/{id_}/events?follow=false").text
    assert '"content_source": "buyer_confirmed"' in events and '"type": "vendor.contacted"' in events
    tick(client)
    assert len(fo.asks) == 1  # nothing was asked of Fo again
    assert client.get(f"/jobs/{id_}").json()["state"] == "RUNNING"


@pytest.mark.parametrize("body,code", [
    ({"message_id": "m_01", "outcome": "retry"}, 422),                    # the buyer must confirm they checked
    ({"message_id": "m_01", "outcome": "retry", "checked": False}, 422),
    ({"message_id": "m_01", "outcome": "resend", "checked": True}, 422),
    ({"message_id": "m_02", "outcome": "retry", "checked": True}, 409),   # not the paused message
])
def test_resolution_requires_confirmation_and_the_paused_message(live, monkeypatch, body, code):
    client, id_, state = live
    paused(client, id_, monkeypatch, Fo(UNCLEAR))
    assert client.post(f"/jobs/{id_}/resolve", json=body).status_code == code
    assert Store(state).data["active"] is not None
    assert client.get(f"/jobs/{id_}").json()["state"] == "NEEDS_HUMAN"


def test_other_pauses_and_a_different_active_fo_turn_are_not_resolvable(live, monkeypatch):
    client, id_, state = live
    job = paused(client, id_, monkeypatch, Fo(UNCLEAR))
    fo = Store(state)
    fo.data["active"] = json.dumps(["another-job", "run", "m_01"], separators=(",", ":"))
    fo.save()
    response = client.post(f"/jobs/{id_}/resolve", json={"message_id": "m_01", "outcome": "skip", "checked": True})
    assert response.status_code == 409 and response.json()["error"] == "fo_account_requires_review"
    assert client.get(f"/jobs/{id_}").json()["state"] == "NEEDS_HUMAN"
    # A later, unrelated blocking pause hides the Continue option.
    svc, executor = client.app.state.service, client.app.state.executor
    def guard_pause():
        with svc.store.transaction():
            eng = svc.engine(id_)
            eng.log.emit(1, "job.needs_human", None, {"reason_code": "inbound_identity_review", "blocking": True, "message": "x"})
            svc.store._save(svc.account, eng)
    executor.submit(guard_pause).result(3)
    assert client.get(f"/jobs/{id_}").json()["review"] is None and job["review"] is not None


def test_slow_call_plan_is_polled_instead_of_paused(tmp_path, message):
    provider = Provider(message)
    plan = json.dumps(provider.plan)
    replies = [{"status": "working", "conversationId": "existing-call"},
               {"status": "complete", "conversationId": "existing-call", "response": plan}]
    first_ask = provider.ask
    def ask(text, cid):
        result = first_ask(text, cid)
        return {"status": "working", "conversationId": "existing-call"} if len(provider.asks) == 1 else result
    provider.ask = ask
    provider.reply = lambda cid, wait_seconds=40: replies.pop(0)
    result = CallTransport(provider, tmp_path / "fo.json", sleep=lambda s: None).send(
        0, message, lambda preview: True)
    assert result.pending_call and result.reason_code is None
    assert len(provider.asks) == 2 and not replies  # plan once, dial permission once


def test_plan_that_never_finishes_fails_closed_without_asking_again(tmp_path, message):
    provider = Provider(message)
    provider.ask = lambda text, cid: (provider.asks.append((text, cid)), {"status": "working", "conversationId": "existing-call"})[1]
    provider.reply = lambda cid, wait_seconds=40: {"status": "working", "conversationId": cid}
    times = iter(range(0, 10_000, 100))
    result = CallTransport(provider, tmp_path / "fo.json", clock=lambda: next(times), sleep=lambda s: None).send(
        0, message, lambda preview: True)
    assert result.reason_code == "send_unclear" and len(provider.asks) == 1
    assert Store(tmp_path / "fo.json").data["active"] is not None


@pytest.fixture
def message():
    from tests.test_call_lifecycle import message as fixture
    return fixture.__wrapped__()


def wrapped(record, before="Plan verified and staged, nothing dialed or scheduled. The number checks out "
            "(685 Bethlehem Pike, open 9-8 today). Here it is back, unchanged:",
            after="I'm holding until your sealed instruction to place the one call."):
    """The shape Fo actually returns: prose, one fenced JSON block, prose."""
    return f"{before}\n\n```json\n{record}\n```\n\n{after}"


def test_plan_wrapped_in_fo_prose_is_accepted_and_dialed_once(tmp_path, message):
    provider = Provider(message)
    provider.plan = wrapped(json.dumps(provider.plan))
    first_ask = provider.ask
    provider.ask = lambda text, cid: {**first_ask(text, cid), **(
        {"response": provider.plan} if len(provider.asks) == 1 else {})}
    result = CallTransport(provider, tmp_path / "fo.json").send(0, message, lambda preview: True)
    assert result.pending_call and result.reason_code is None and len(provider.asks) == 2


@pytest.mark.parametrize("text", [
    lambda plan: wrapped(plan, before="I've called them already. Here is the plan:"),
    lambda plan: wrapped(plan, after="I have scheduled a retry for tomorrow."),
    lambda plan: wrapped(plan) + "\n```json\n" + plan + "\n```",          # two records
])
def test_wrapped_plan_with_claims_or_extra_records_is_never_dialed(tmp_path, message, text):
    provider = Provider(message)
    plan = json.dumps(expected_plan(message).model_dump())
    provider.ask = lambda t, cid: (provider.asks.append(t), {"status": "complete", "conversationId": "c", "response": text(plan)})[1]
    from sealed.core.guard import Guard
    from tests.helpers import FakeChecker
    result = CallTransport(provider, tmp_path / "fo.json").send(
        0, message, lambda preview: Guard(FakeChecker()).check(message, preview=preview).passed)
    assert not result.sent and not result.pending_call and len(provider.asks) == 1


def test_call_report_wrapped_in_fo_prose_completes_the_call(tmp_path, message):
    from tests.test_call_lifecycle import report
    provider = Provider(message)
    assert CallTransport(provider, tmp_path / "fo.json").send(0, message, lambda preview: True).pending_call
    provider.turns = [{"role": "assistant", "at": "2999-01-01T00:00:00+00:00",
                       "text": wrapped(report(message).model_dump_json(), before="Call finished. Report:", after="Done.")}]
    result = CallTransport(provider, tmp_path / "fo.json").poll_call(1, message)
    assert result.sent and result.reason_code is None and result.call_report["outcome"] == "connected"


def test_plan_inline_without_a_fence_is_accepted(message):
    from sealed.core.call_policy import plan_matches
    plan = json.dumps(expected_plan(message).model_dump())
    assert plan_matches(message, f"Plan staged, nothing dialed or scheduled. Here it is back, unchanged:\n\n{plan}\nHolding for the sealed instruction.")
    assert not plan_matches(message, f"Here it is: {plan} and again {plan}")
    assert not plan_matches(message, f"I've called them. {plan}")


def test_stopped_payment_language_draft_offers_continue(tmp_path, monkeypatch):
    from sealed.core.email_policy import format_draft
    from sealed.core.models import EmailDraft
    from sealed.service.sim_demo import Extractor, Mailbox
    monkeypatch.setenv("GMAIL_ADDRESS", "buyer@example.com")
    monkeypatch.setattr("app.jobs.GmailReader", lambda *a: nullcontext(Mailbox()))
    monkeypatch.setattr("app.jobs.QuoteParser", lambda *a: nullcontext(Extractor()))
    with local(create_app(data_dir=tmp_path, mode="live", background=False, fo_state=tmp_path / "fo.json")) as client:
        id_ = client.post("/jobs", json=payload(buyer="John", vendors=[
            {"name": "Email seller", "email": "seller@example.org", "mode": "live"}])).json()["id"]
        assert client.post(f"/jobs/{id_}/start", json={"allow_model_requests": True, "allow_dispatch": True}).status_code == 200
        svc, executor = client.app.state.service, client.app.state.executor
        msg = executor.submit(lambda: svc.engine(id_).outbound["m_01"]).result(3)
        draft = format_draft(EmailDraft(msg.to, msg.cc, msg.subject, "I'm Fo, an AI assistant for John. "
                                        "No purchase is being arranged at this stage. Please reply-all."))
        asks = []
        class Email:
            def ask(self, text, cid):
                asks.append(text)
                return {"status": "complete", "conversationId": "mail", "response": draft if len(asks) == 1 else
                        "Understood, I won't send anything."}
            def close(self):
                pass
        monkeypatch.setattr("sealed.service.live.LiveFoClient", Email)
        executor.submit(svc.tick).result(3)
        job = client.get(f"/jobs/{id_}").json()
        assert job["state"] == "NEEDS_HUMAN" and job["review"]["reason_code"] == "payment_mention"
        assert "don't send" in asks[1]  # Sealed told Fo not to send
        assert client.post(f"/jobs/{id_}/resolve", json={"message_id": "m_01", "outcome": "retry", "checked": True}).status_code == 200
        retried = executor.submit(lambda: svc.engine(id_).outbound["m_02"]).result(3)
        assert "not even in a negative" in retried.fo_instruction and Store(tmp_path / "fo.json").data["active"] is None
