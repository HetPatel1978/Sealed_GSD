import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from sealed.core.engine import Engine
from sealed.core.models import Decision
from sealed.core.guard import Guard
from sealed.fo import messages
from sealed.fo.adapter import CONFIRM_NO, CONFIRM_YES, FoTransport, classify
from sealed.fo.client import FoTimeout
from tests.helpers import FakeChecker, job

# Real Fo replies observed in spikes 1b, 1c, 1d (2026-10-17)
SENT_1C = ("Sent. It went from your Gmail to rfb5381@psu.edu with the exact subject and body, and you're on CC. "
           "I've noted it so the reply lands where I can pick it up for you.")
CALL_1D = ("Call's dispatched to your phone now, and I've set myself a check at 4:58 PM to make sure the transcript "
           "summary reaches you one way or another, even if the auto-report hiccups.")
REFUSED_1B = ("The send got stopped on my end: the message is written in your voice and signed \"Het,\" and I can "
              "only send openly as your assistant, not as you. Two ways forward: ... Want me to go with option 1?")


class FakeFo:
    """Scripted Fo. `script` is a list of responses (dicts, or exceptions to raise) consumed in order."""

    def __init__(self, script):
        self.script = list(script)
        self.asks: list[tuple[str, str | None]] = []
        self.polls = 0
        self.reply_ids = []
        self._busy = False
        self._n = 0

    def _next(self, cid):
        if self._busy:
            raise AssertionError("concurrent Fo turns")
        self._busy = True
        try:
            time.sleep(0.01)
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if cid is None:
                self._n += 1
                cid = f"conv-{self._n}"
            return {"conversationId": cid, **item}
        finally:
            self._busy = False

    def ask(self, message, conversation_id):
        self.asks.append((message, conversation_id))
        return self._next(conversation_id)

    def reply(self, conversation_id, wait_seconds=40):
        self.polls += 1
        self.reply_ids.append(conversation_id)
        return self._next(conversation_id)

    def conversation(self, conversation_id):
        return {"conversation": {"conversationId": conversation_id}}

    def conversations(self):
        return []


J = job()


def msg(dealer="a", kind="rfq", channel="email", mid="m_01"):
    return messages.build(J, Decision(dealer, "rfq", kind, channel=channel), mid, "SEALED-T0000001", "buyer@x.example")


def ok(text):
    return {"status": "complete", "response": text}


@pytest.fixture
def store(tmp_path):
    return tmp_path / "fo_store.json"


def test_classify_real_fo_replies():
    assert classify(SENT_1C) == "sent"
    assert classify("Sent, exactly as written. You're on CC.") == "sent"
    assert classify(CALL_1D) == "sent"
    assert classify(REFUSED_1B) == "refused"
    assert classify("Here's the draft I'll send to Alpha Motors: ... Good to send?") == "question"
    assert classify("Working on it.") == "unclear"


def test_sends_and_keeps_one_conversation_per_dealer(store):
    fo = FakeFo([ok(SENT_1C), ok(SENT_1C), ok(SENT_1C)])
    t = FoTransport(fo, store)
    assert t.send(0, msg("a", mid="m_01"), lambda p: True).sent
    assert t.send(1, msg("a", "firm_up", mid="m_02"), lambda p: True).sent
    assert t.send(2, msg("b", mid="m_03"), lambda p: True).sent
    assert [cid for _, cid in fo.asks] == [None, "conv-1", None]      # new, reuse, new


def test_working_is_polled_never_resent(store):
    fo = FakeFo([{"status": "working", "progress": "Drafting"}, {"status": "working"}, ok(SENT_1C)])
    res = FoTransport(fo, store).send(0, msg(), lambda p: True)
    assert res.sent and len(fo.asks) == 1 and fo.polls == 2


def test_timeout_is_polled_never_resent(store):
    fo = FakeFo([FoTimeout("ask_fo timed out"), ok(SENT_1C)])
    res = FoTransport(fo, store).send(0, msg(), lambda p: True)
    assert res.sent and len(fo.asks) == 1 and fo.polls == 1
    assert res.errors[0]["code"] == "fo_turn_timeout" and res.errors[0]["resent"] is False


def test_preview_is_checked_and_confirmed_only_if_it_passes(store):
    m = msg()
    preview = f"Here's what I'll send to Alpha Motors:\n{m.body}\nGood to send?"
    seen = []
    fo = FakeFo([ok(preview), ok(SENT_1C)])
    res = FoTransport(fo, store).send(0, m, lambda p: seen.append(p) or True)
    assert res.sent and seen == [preview] and fo.asks[1][0] == CONFIRM_YES


def test_failed_preview_check_tells_fo_not_to_send(store):
    fo = FakeFo([ok("I tweaked the wording a bit. Good to send?"), ok("Okay, I won't send it.")])
    res = FoTransport(fo, store).send(0, msg(), lambda p: False)
    assert not res.sent and res.reason_code == "audit_mismatch" and fo.asks[1][0] == CONFIRM_NO


def test_refusal_is_not_sent(store):
    res = FoTransport(FakeFo([ok(REFUSED_1B)]), store).send(0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "fo_refused" and "stopped" in res.note


def test_same_message_is_never_sent_twice_even_after_restart(store):
    fo = FakeFo([ok(SENT_1C)])
    FoTransport(fo, store).send(0, msg(mid="m_07"), lambda p: True)
    again = FoTransport(fo, store).send(5, msg(mid="m_07"), lambda p: True)     # new instance, same store
    assert len(fo.asks) == 1 and again.errors[0]["code"] == "duplicate_suppressed"


def test_unresolved_turn_after_poll_limit_is_unclear_and_never_resent(store):
    clock = iter(range(0, 10_000, 400)).__next__
    fo = FakeFo([{"status": "working"}] * 5)
    t = FoTransport(fo, store, poll_limit_s=600, clock=lambda: float(clock()))
    res = t.send(0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "send_unclear"
    assert t.send(1, msg(), lambda p: True).errors[0]["code"] == "duplicate_suppressed"
    assert len(fo.asks) == 1


def test_payment_mention_stops(store):
    res = FoTransport(FakeFo([ok("I went ahead and placed the order and paid the deposit.")]), store).send(
        0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "payment_mention"


def test_turns_are_serial_across_threads(store):
    fo = FakeFo([ok(SENT_1C)] * 6)
    t = FoTransport(fo, store)
    threads = [threading.Thread(target=t.send, args=(0, msg(d, mid=f"m_{i}"), lambda p: True))
               for i, d in enumerate("abcabc")]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert len(fo.asks) == 6        # FakeFo raises on any overlapping turn


def test_engine_runs_round_one_through_the_adapter(store):
    fo = FakeFo([ok(SENT_1C)] * 3)
    eng = Engine(J, FoTransport(fo, store), FakeChecker(), qualifier=None, cc="buyer@x.example", tag="SEALED-T0000001")
    eng.create(0)
    eng.start(0.1)
    sent = eng.log.of_type("message.sent")
    assert [e["vendor_id"] for e in sent] == ["a", "b", "c"]
    assert all(cid is None for _, cid in fo.asks)                       # each dealer opens its own conversation
    assert not eng.log.of_type("audit.checked")                         # audit waits for the Gmail CC copy (step 3)


def test_engine_raises_needs_human_when_fo_refuses(store):
    fo = FakeFo([ok(REFUSED_1B)])
    eng = Engine(J, FoTransport(fo, store), FakeChecker(), qualifier=None, cc="buyer@x.example", tag="SEALED-T0000001")
    eng.create(0)
    eng.start(0.1)
    nh = eng.log.of_type("job.needs_human")
    assert nh and nh[0]["data"]["reason_code"] == "fo_refused" and "fo_said" in nh[0]["data"]
    assert eng.state == "NEEDS_HUMAN" and not eng.log.of_type("message.sent")


def test_same_message_is_atomic_across_threads_and_preexisting_transport_instances(store):
    fo = FakeFo([ok("Sent.")] * 2)
    transports = [FoTransport(fo, store), FoTransport(fo, store)]
    start = threading.Barrier(2)
    def send(t):
        start.wait(timeout=5)
        return t.send(0, msg(), lambda p: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, transports))
    assert all(r.sent for r in results) and len(fo.asks) == 1
    assert sum(bool(r.errors) for r in results) == 1


def test_different_messages_share_one_account_queue_across_transport_instances(store):
    fo = FakeFo([ok("Sent.")] * 2)
    transports = [FoTransport(fo, store), FoTransport(fo, store)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: transports[i].send(0, msg(mid=f"m_{i}"), lambda p: True), range(2)))
    assert all(r.sent for r in results) and len(fo.asks) == 2
    assert [cid for _, cid in fo.asks] == [None, "conv-1"]
    assert len(json.loads(store.read_text())["messages"]) == 2


@pytest.mark.parametrize("scope", [{"job_id": "other-job"}, {"run_id": "other-run"}])
def test_message_ids_and_dealer_conversations_are_scoped_to_job_and_run(store, scope):
    fo = FakeFo([ok("Sent.")] * 3)
    t = FoTransport(fo, store)
    assert t.send(0, msg(), lambda p: True).sent
    assert t.send(1, replace(msg(), **scope), lambda p: True).sent
    assert t.send(2, replace(msg(mid="m_02"), **scope), lambda p: True).sent
    assert [cid for _, cid in fo.asks] == [None, None, "conv-2"]


def test_a_reused_message_id_with_a_changed_payload_is_not_reported_as_sent(store):
    fo = FakeFo([ok("Sent.")])
    t = FoTransport(fo, store)
    t.send(0, msg(), lambda p: True)
    res = t.send(1, replace(msg(), to="another@example.com"), lambda p: True)
    assert not res.sent and res.errors[0]["code"] == "message_id_conflict" and len(fo.asks) == 1


def test_unscoped_messages_never_reach_fo(store):
    fo = FakeFo([])
    with pytest.raises(ValueError, match="job_id and run_id"):
        FoTransport(fo, store).send(0, replace(msg(), job_id=""), lambda p: True)
    assert not fo.asks


def test_pending_reservation_is_on_disk_before_the_first_ask(store):
    class CheckPending(FakeFo):
        def ask(self, message, conversation_id):
            data = json.loads(store.read_text())
            assert data["version"] == 2
            assert data["messages"][data["active"]]["status"] == "pending"
            return super().ask(message, conversation_id)
    assert FoTransport(CheckPending([ok("Sent.")]), store).send(0, msg(), lambda p: True).sent
    assert json.loads(store.read_text())["active"] is None


def test_late_conversation_id_is_saved_and_used_for_polling_confirmation_and_followups(store):
    m = msg()
    fo = FakeFo([FoTimeout("lost first response"), {"status": "working", "conversationId": "late"},
                 {**ok(m.body + "\nGood to send?"), "conversationId": "late"}, ok("Sent."), ok("Sent.")])
    t = FoTransport(fo, store, sleep=lambda _: None)
    assert t.send(0, m, lambda p: True).sent
    assert t.send(1, msg(kind="firm_up", mid="m_02"), lambda p: True).sent
    assert fo.reply_ids == [None, "late"]
    assert [cid for _, cid in fo.asks] == [None, "late", "late"]


def test_a_reply_timeout_keeps_polling_the_same_conversation_without_resending(store):
    fo = FakeFo([{"status": "working"}, FoTimeout("reply timeout"), ok("Sent.")])
    assert FoTransport(fo, store).send(0, msg(), lambda p: True).sent
    assert len(fo.asks) == 1 and fo.reply_ids == ["conv-1", "conv-1"]


def test_unresolved_turn_blocks_new_messages_even_after_restart(store):
    clock = iter(range(0, 10000, 400)).__next__
    fo = FakeFo([{"status": "working"}] * 4)
    t = FoTransport(fo, store, clock=lambda: float(clock()), sleep=lambda _: None)
    assert not t.send(0, msg(), lambda p: True).sent
    again = FoTransport(fo, store).send(1, replace(msg(mid="new"), job_id="other"), lambda p: True)
    assert not again.sent and again.errors[0]["code"] == "fo_turn_unresolved"
    assert len(fo.asks) == 1


def test_unexpected_conversation_is_not_confirmed_or_saved_as_the_dealers_conversation(store):
    fo = FakeFo([ok("Sent."), {**ok("Good to send?"), "conversationId": "wrong"}])
    t = FoTransport(fo, store)
    assert t.send(0, msg(), lambda p: True).sent
    res = t.send(1, msg(mid="m_02"), lambda p: True)
    assert not res.sent and res.reason_code == "send_unclear" and len(fo.asks) == 2
    assert t.store.conversation(msg()) == "conv-1"


def test_a_question_without_a_recovered_conversation_id_never_opens_a_new_conversation(store):
    fo = FakeFo([FoTimeout("lost"), {**ok(msg().body + " Good to send?"), "conversationId": None}])
    res = FoTransport(fo, store).send(0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "send_unclear" and len(fo.asks) == 1


def test_payment_instructions_are_rejected_before_confirmation_even_if_callback_allows_them(store):
    fo = FakeFo([ok("Please pay the deposit. Good to send?"), ok("Okay, I won't send it.")])
    res = FoTransport(fo, store).send(0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "payment_mention"
    assert [text for text, _ in fo.asks][1:] == [CONFIRM_NO]


def test_verbatim_approved_booking_preview_is_not_mistaken_for_a_payment_action(store):
    m = messages.build(J, Decision("a", "propose", "booking"), "booking", "TAG", "buyer@example.com", 30450)
    fo = FakeFo([ok(m.body + " Good to send?"), ok("Sent.")])
    res = FoTransport(fo, store).send(0, m, lambda p: Guard(FakeChecker()).check(m, preview=p).passed)
    assert res.sent and res.reason_code is None and fo.asks[1][0] == CONFIRM_YES


def test_sent_payment_warning_is_preserved_when_a_duplicate_is_suppressed(store):
    fo = FakeFo([ok("Sent. I paid the deposit.")])
    res = FoTransport(fo, store).send(0, msg(), lambda p: True)
    again = FoTransport(fo, store).send(1, msg(), lambda p: True)
    assert res.sent and again.sent
    assert res.reason_code == again.reason_code == "payment_mention" and len(fo.asks) == 1


def test_sending_during_preview_cancellation_pauses_the_engine(store):
    fo = FakeFo([ok("I changed the draft. Good to send?"), ok("Sent.")])
    eng = Engine(J, FoTransport(fo, store), FakeChecker(), qualifier=None, cc="buyer@example.com", tag="TAG")
    eng.start(0)
    assert eng.state == "NEEDS_HUMAN" and len(fo.asks) == 2
    assert eng.log.of_type("job.needs_human")[-1]["data"]["reason_code"] == "audit_mismatch"


def test_network_exception_is_sanitized_and_never_retried(store):
    fo = FakeFo([RuntimeError("Authorization: Bearer PRIVATE_TOKEN")])
    t = FoTransport(fo, store)
    res = t.send(0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "send_unclear"
    assert "PRIVATE_TOKEN" not in repr(res) + store.read_text()
    assert not t.send(1, msg(mid="next"), lambda p: True).sent and len(fo.asks) == 1


def test_legacy_state_is_preserved_and_requires_review_instead_of_guessing_a_migration(store):
    data = json.dumps({"messages": {"m_01": {"status": "sent"}}, "conversations": {"a": "old"}})
    store.write_text(data)
    with pytest.raises(ValueError, match="Legacy unscoped"):
        FoTransport(FakeFo([]), store)
    assert store.read_text() == data


def test_state_write_failure_before_reservation_never_contacts_fo(store, monkeypatch):
    import sealed.fo.adapter as adapter
    def fail(*args):
        raise OSError("write denied")
    monkeypatch.setattr(adapter.os, "replace", fail)
    fo = FakeFo([])
    res = FoTransport(fo, store).send(0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "integration_down" and not fo.asks
    assert not store.exists() and not list(store.parent.glob("*.tmp"))


@pytest.mark.parametrize("text", ["Sentinel is ready.", "Sent?", "Sent the draft?",
                                 "Done drafting. Good to send?", "Done. Shall I send it?", "Working on it."])
def test_incomplete_or_questionable_status_text_is_never_classified_as_sent(text):
    assert classify(text) != "sent"


def test_missing_sent_conversation_id_flags_the_send_and_blocks_further_turns(store):
    fo = FakeFo([{**ok("Sent."), "conversationId": None}])
    t = FoTransport(fo, store)
    res = t.send(0, msg(), lambda p: True)
    assert res.sent and res.reason_code == "send_unclear"
    assert not t.send(1, msg(mid="next"), lambda p: True).sent and len(fo.asks) == 1


def test_failure_to_save_after_send_keeps_the_original_reservation_and_does_not_resend(store, monkeypatch):
    import sealed.fo.adapter as adapter
    real_replace = adapter.os.replace
    calls = 0
    def fail_after_reservation(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("disk failure after the original request")
        return real_replace(*args)
    fo = FakeFo([ok("Sent.")])
    with monkeypatch.context() as patch:
        patch.setattr(adapter.os, "replace", fail_after_reservation)
        res = FoTransport(fo, store).send(0, msg(), lambda p: True)
    assert not res.sent and res.reason_code == "send_unclear"
    again = FoTransport(fo, store).send(1, msg(), lambda p: True)
    assert not again.sent and len(fo.asks) == 1
    assert json.loads(store.read_text())["active"] is not None


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_poll_limit_must_be_finite_and_positive(store, value):
    with pytest.raises(ValueError):
        FoTransport(FakeFo([]), store, poll_limit_s=value)
