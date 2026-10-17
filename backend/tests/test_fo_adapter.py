import threading
import time

import pytest

from sealed.core.engine import Engine
from sealed.core.models import Decision
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
