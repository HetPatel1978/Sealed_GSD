"""Controlled call lifecycle acceptance: no network, credentials or dealer outreach."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sealed.core.call_policy import Report, audit_report, expected_plan, opening, request_id
from sealed.core.guard import Guard
from sealed.core.models import Decision, outbound_fingerprint
from sealed.fo.adapter import Store
from sealed.fo.calls import CallTransport
from sealed.fo.client import FoTimeout
from sealed.fo.messages import build
from tests.helpers import FakeChecker, job


@pytest.fixture
def message():
    return build(replace(job(), buyer="Hari"), Decision("a", "rfq", "rfq", channel="call"),
                 "m_01", "SEALED-CALL-TEST", "buyer@example.com")


def report(msg, **changes):
    return Report.model_validate({"state": "completed", "request_id": request_id(msg), "phone": msg.to,
        "buyer": msg.call_buyer, "opening_used": opening(msg.call_buyer), "followups_scheduled": False,
        "outcome": "connected", "transcript_complete": True,
        "transcript": [{"role": "assistant", "text": opening(msg.call_buyer) + " Could you email the written quote?"},
                       {"role": "dealer", "text": "The total is $30,000, but I will send the written quote later."}], **changes})


class Provider:
    def __init__(self, msg):
        self.msg, self.asks, self.reads = msg, [], []
        self.plan = expected_plan(msg).model_dump()
        self.started = "On it, calling Alpha Motors now. I'll report back here after it ends."
        self.turns = []

    def ask(self, text, cid):
        self.asks.append((text, cid))
        assert len(self.asks) <= 2, "Call permission was repeated"
        response = json.dumps(self.plan) if len(self.asks) == 1 else self.started
        if isinstance(response, Exception):
            raise response
        return {"status": "complete", "conversationId": "existing-call", "response": response}

    def conversation(self, cid):
        self.reads.append(cid)
        return {"conversation": {"conversationId": cid}, "turns": self.turns}


def send(provider, path, msg):
    return CallTransport(provider, path).send(0, msg, lambda preview: Guard(FakeChecker()).check(msg, preview=preview).passed)


def test_acknowledgement_stays_pending_and_restart_only_observes_existing_call(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    result = send(provider, path, message)
    assert result.pending_call and not result.sent and result.reason_code is None
    assert len(provider.asks) == 2 and provider.asks[0][1] is None and provider.asks[1][1] == "existing-call"
    assert "Do not dial" in provider.asks[0][0]
    assert "Do not email, retry" in provider.asks[1][0]
    restarted = CallTransport(provider, path)
    assert restarted.poll_call(1, message).pending_call
    provider.turns = [{"role": "assistant", "text": report(message).model_dump_json()}]
    completed = restarted.poll_call(2, message)
    assert completed.sent and completed.reason_code is None
    assert completed.call_report["identity_verified"] and completed.call_report["outcome"] == "connected"
    assert "30,000" not in repr(completed)
    assert Store(path).data["active"] is None
    assert restarted.poll_call(3, message).sent  # crash/replay of a saved result
    assert len(provider.asks) == 2 and len(provider.reads) == 2


@pytest.mark.parametrize("change", [{"buyer": "Account owner"}, {"opening": "I'm the account owner's assistant."},
                                   {"phone": "+15550009999"}, {"brief": "Changed the requested car"},
                                   {"followups_scheduled": True}, {"call_started": True}])
def test_unauthorized_plan_never_gets_permission_to_dial(tmp_path, message, change):
    provider = Provider(message)
    provider.plan.update(change)
    result = send(provider, tmp_path / "fo.json", message)
    assert not result.sent and not result.pending_call and result.reason_code
    assert len(provider.asks) == 1


@pytest.mark.parametrize("change,flag", [
    ({"buyer": "Account owner"}, "buyer_identity_mismatch"),
    ({"opening_used": "I'm the account owner's assistant."}, "buyer_identity_mismatch"),
    ({"followups_scheduled": True}, "unrequested_followup"),
    ({"transcript_complete": False}, "call_evidence_missing"),
    ({"transcript": []}, "buyer_identity_unverified"),
    ({"transcript": [{"role": "assistant", "text": "Hi, this is Het Patel's assistant."}]}, "buyer_identity_unverified"),
    ({"transcript": [{"role": "assistant", "text": opening("Hari") + " I will retry tomorrow."}]}, "unrequested_followup"),
])
def test_bad_postcall_evidence_pauses_and_keeps_reservation(tmp_path, message, change, flag):
    provider, path = Provider(message), tmp_path / "fo.json"
    assert send(provider, path, message).pending_call
    provider.turns = [{"role": "assistant", "text": report(message, **change).model_dump_json()}]
    result = CallTransport(provider, path).poll_call(2, message)
    assert result.reason_code == "call_review"
    if result.call_report:
        assert flag in result.call_report["flags"]
    assert Store(path).data["active"]
    assert len(provider.asks) == 2


def test_old_report_and_user_provided_report_do_not_complete_current_call(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    send(provider, path, message)
    provider.turns = [{"role": "assistant", "text": report(message, request_id="older-call").model_dump_json()},
                      {"role": "user", "text": report(message).model_dump_json()}]
    assert CallTransport(provider, path).poll_call(2, message).pending_call
    assert Store(path).data["active"]


def test_wrong_conversation_is_not_observed_as_a_completed_call(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    send(provider, path, message)
    provider.conversation = lambda cid: {"conversation": {"conversationId": "other"},
        "turns": [{"role": "assistant", "text": report(message).model_dump_json()}]}
    assert CallTransport(provider, path).poll_call(2, message).pending_call
    assert Store(path).data["active"]


def test_read_failure_then_completion_never_repeats_permission(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    send(provider, path, message)
    original = provider.conversation
    provider.conversation = lambda cid: (_ for _ in ()).throw(FoTimeout("read failed"))
    assert CallTransport(provider, path).poll_call(2, message).pending_call
    provider.conversation = original
    provider.turns = [{"role": "assistant", "text": report(message).model_dump_json()}]
    assert CallTransport(provider, path).poll_call(3, message).sent
    assert len(provider.asks) == 2


def test_pending_call_deadline_requires_review_instead_of_retry(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    send(provider, path, message)
    state = Store(path)
    state.data["messages"][state.data["active"]]["deadline"] = 0
    state.save()
    result = CallTransport(provider, path).poll_call(2, message)
    assert result.reason_code == "call_result_missing" and not result.sent
    assert Store(path).data["active"] and len(provider.asks) == 2 and not provider.reads


def test_late_fo_scheduling_report_raises_review(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    send(provider, path, message)
    provider.turns = [{"role": "assistant", "at": (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat(),
                      "text": "Scheduling the retry for tomorrow."}]
    assert CallTransport(provider, path).poll_call(2, message).reason_code == "call_review"
    assert Store(path).data["active"]


def test_no_answer_is_a_finished_attempt_and_has_no_firm_quote(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    send(provider, path, message)
    provider.turns = [{"role": "assistant", "text": report(message, outcome="no_answer", opening_used=None, transcript=[]).model_dump_json()}]
    result = CallTransport(provider, path).poll_call(2, message)
    assert result.sent and result.reason_code is None and result.call_report["outcome"] == "no_answer"
    assert result.call_report["identity_status"] == "not_spoken" and not result.call_report["identity_verified"]
    assert Store(path).data["active"] is None


def test_legacy_fingerprint_and_uncertain_record_are_not_migrated_or_retried(tmp_path, message):
    provider, path = Provider(message), tmp_path / "fo.json"
    state = Store(path)
    key = Store.key(message.job_id, message.run_id, message.message_id)
    state.set_status(key, "unclear", active=True, fingerprint="legacy-hash", channel="call")
    before = path.read_bytes()
    assert not send(provider, path, message).sent
    assert path.read_bytes() == before and not provider.asks
    assert outbound_fingerprint(replace(message, call_buyer=None)) != outbound_fingerprint(message)


def test_unicode_buyer_identity_cannot_be_normalized_to_another_name(message):
    msg = replace(message, call_buyer="\u5f20\u4e09")
    result = report(msg, opening_used=opening("\u674e\u56db"))
    assert "buyer_identity_mismatch" in audit_report(msg, result)


@pytest.mark.parametrize("error", [FoTimeout("call request timed out"), RuntimeError("Authorization: Bearer PRIVATE_TOKEN")])
def test_exception_after_dial_permission_only_polls_the_known_call(tmp_path, message, error):
    provider, path = Provider(message), tmp_path / "fo.json"
    provider.started = error
    result = send(provider, path, message)
    assert result.pending_call and "PRIVATE_TOKEN" not in repr(result) + path.read_text()
    provider.turns = [{"role": "assistant", "text": report(message).model_dump_json()}]
    assert CallTransport(provider, path).poll_call(2, message).sent
    assert len(provider.asks) == 2


def test_duplicate_plan_keys_are_never_approved(tmp_path, message):
    provider = Provider(message)
    original = provider.ask
    def ask(text, cid):
        result = original(text, cid)
        result["response"] = result["response"].replace('"buyer": "Hari"', '"buyer":"Account owner","buyer":"Hari"')
        return result
    provider.ask = ask
    result = send(provider, tmp_path / "fo.json", message)
    assert not result.sent and not result.pending_call and len(provider.asks) == 1
