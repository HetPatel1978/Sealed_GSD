"""Persisted call lifecycle through the real coordinator and transactional outbox."""

from dataclasses import replace

import pytest

from sealed.core.models import Dealer
from sealed.fo.calls import CallTransport
from sealed.private import vault
from sealed.service.coordinator import Policy
from tests.test_call_lifecycle import Provider, report
from tests.test_coordinator import Rig


class Transport:
    def __init__(self, path):
        self.path, self.provider = path / "fo.json", None

    def send(self, now, msg, approve):
        assert self.provider is None, "A second call was attempted"
        self.provider = Provider(msg)
        return CallTransport(self.provider, self.path).send(now, msg, approve)

    def poll_call(self, now, msg):
        return CallTransport(self.provider, self.path).poll_call(now, msg)


def configured(path):
    rig = Rig(path, policy=Policy(True, True, True), register=False)
    rig.job = replace(rig.job, buyer="Hari", mode="live", dealers=(
        Dealer("seller", "Synthetic call dealer", None, "+1-215-555-0101", mode="live"),))
    rig.transport = Transport(path)
    rig.co.register(rig.job, cc=rig.reader.address, tag="SEALED-CALL-ACCEPTANCE")
    return rig


@pytest.fixture
def rig(tmp_path):
    result = configured(tmp_path)
    yield result
    result.close()


def pending(rig):
    rig.tick()  # public intent, no dialing yet
    assert rig.transport.provider is None
    outcome = rig.tick()  # checked plan, one dial permission, then progress
    assert outcome["runs"][0]["status"] == "call_in_progress"
    assert len(rig.transport.provider.asks) == 2
    eng = rig.engine()
    assert eng.state == "RUNNING" and len(eng.log.of_type("call.pending")) == 1
    assert not eng.log.of_type("job.needs_human") and not eng.log.of_type("message.sent")
    return next(iter(eng.outbound.values()))


def test_pending_call_holds_timers_then_finishes_without_creating_a_firm_quote(rig):
    msg = pending(rig)
    rig.clock.advance(300)
    assert rig.tick()["status"] == "call_in_progress"
    assert not rig.store.db.execute("SELECT 1 FROM coordinator_timers").fetchone()
    rig.transport.provider.turns = [{"role": "assistant", "text": report(msg).model_dump_json()}]
    rig.tick()
    engine = rig.engine()
    assert len(engine.log.of_type("call.completed")) == 1
    assert len(engine.log.of_type("message.sent")) == 1
    assert not engine.ledger.quotes and engine.proposal is None
    assert not rig.store.pending_calls(rig.reader.address)
    assert len(rig.transport.provider.asks) == 2


def test_calls_are_observed_before_mail_outage_checks(rig):
    msg = pending(rig)
    rig.reader.search = lambda query: (_ for _ in ()).throw(OSError("mail offline"))
    assert rig.tick()["status"] == "call_in_progress"
    rig.transport.provider.turns = [{"role": "assistant", "text": report(msg).model_dump_json()}]
    rig.co.poll_calls((rig.job.id, rig.job.run_id))
    assert not rig.store.pending_calls(rig.reader.address)
    assert len(rig.transport.provider.asks) == 2


def test_recovered_dispatch_claim_is_observed_and_never_resent(rig):
    msg = pending(rig)
    rig.store.db.execute("UPDATE engine_outbox SET status='dispatching'")
    rig.transport.provider.turns = [{"role": "assistant", "text": report(msg).model_dump_json()}]
    rig.tick()
    assert not rig.store.in_flight(rig.reader.address, rig.job.id, rig.job.run_id)
    assert len(rig.engine().log.of_type("call.completed")) == 1
    assert len(rig.transport.provider.asks) == 2


def test_restart_observes_call_without_new_permissions_or_private_ceiling(tmp_path):
    first = configured(tmp_path)
    try:
        msg = pending(first)
        transport, clock = first.transport, first.clock
    finally:
        first.close()
    second = Rig(tmp_path, policy=Policy(), register=False)
    try:
        second.transport, second.clock, second.co.clock = transport, clock, clock
        vault.forget(second.job.id)
        transport.provider.turns = [{"role": "assistant", "text": report(msg).model_dump_json()}]
        second.tick()
        assert not second.store.pending_calls(second.reader.address)
        assert len(second.engine().log.of_type("call.completed")) == 1
        assert not second.engine().ledger.quotes
        assert len(transport.provider.asks) == 2
    finally:
        second.close()


def test_cancelled_job_stays_cancelled_while_existing_call_is_observed(rig):
    msg = pending(rig)
    rig.co.cancel(rig.job.id, rig.job.run_id)
    assert rig.engine().state == "CANCELLED"
    vault.forget(rig.job.id)
    rig.transport.provider.turns = [{"role": "assistant", "text": report(msg, outcome="no_answer", opening_used=None, transcript=[]).model_dump_json()}]
    rig.tick()
    assert rig.engine().state == "CANCELLED" and not rig.store.pending_calls(rig.reader.address)
    assert len(rig.engine().log.of_type("call.completed")) == 1
    assert len(rig.transport.provider.asks) == 2


def test_wrong_identity_pauses_job_and_keeps_it_out_of_the_quote_ledger(rig):
    msg = pending(rig)
    rig.transport.provider.turns = [{"role": "assistant", "text": report(msg, buyer="Account owner").model_dump_json()}]
    rig.tick()
    engine = rig.engine()
    assert engine.state == "NEEDS_HUMAN" and not engine.ledger.quotes
    assert engine.log.of_type("job.needs_human")[-1]["data"]["reason_code"] == "call_review"
    assert not rig.store.pending_calls(rig.reader.address)


def test_another_job_waits_while_the_account_has_a_pending_call(rig):
    pending(rig)
    other = replace(rig.job, id="other-job")
    vault.store(other.id, 150)
    try:
        rig.co.register(other, cc=rig.reader.address, tag="SEALED-OTHER-JOB")
        result = rig.co.tick(scopes=[(other.id, other.run_id)])
        assert result["status"] == "fo_busy_call" and result["dispatched"] == 0
        assert not rig.store.outbox(rig.reader.address, other.id, other.run_id)
        assert len(rig.transport.provider.asks) == 2
    finally:
        vault.forget(other.id)


def test_complete_controlled_email_call_quote_approval_confirmation_flow(tmp_path):
    from sealed.core.engine import Waits
    from sealed.service.sim_demo import SELLER
    rig = Rig(tmp_path, policy=Policy(True, True, True), register=False)
    email_transport = rig.transport
    calls = Transport(tmp_path)
    class Mixed:
        rfq = None
        def send(self, now, msg, approve):
            if msg.channel == "call":
                return calls.send(now, msg, approve)
            result = email_transport.send(now, msg, approve)
            if msg.kind == "rfq":
                self.rfq = (msg, result.audit.evidence.message_id)
                rig.reader.mail.clear()  # no immediate reply; exercise the call nudge
            return result
        def poll_call(self, now, msg):
            return calls.poll_call(now, msg)
    mixed = Mixed()
    rig.transport = mixed
    rig.job = replace(rig.job, mode="live", buyer="Hari", dealers=(Dealer("seller", "Synthetic seller", SELLER, "+1-215-555-0101", mode="live"),))
    try:
        rig.co.register(rig.job, cc=rig.reader.address, tag="SEALED-CONTROLLED-FLOW",
                        waits=Waits(nudge_after=1, round1_close=4, round2_call_after=2, round2_close=8))
        for _ in range(4):
            rig.tick()
            if calls.provider:
                break
        assert calls.provider and rig.store.pending_calls(rig.reader.address)
        call_msg = next(m for m in rig.engine().outbound.values() if m.channel == "call")
        calls.provider.turns = [{"role": "assistant", "text": report(call_msg).model_dump_json()}]
        initial, sent_id = mixed.rfq
        rig.reader.reply(initial, price=100, booking=False, sent_id=sent_id, clock=rig.clock)
        proposal = rig.until_approval()
        assert proposal["price_total"] == 90 and not any(m.kind == "booking" for m in rig.engine().outbound.values())
        assert len(rig.engine().log.of_type("call.completed")) == 1
        assert len(calls.provider.asks) == 2
        echo = {k:proposal[k] for k in ("proposal_id", "vendor_id", "price_total", "currency", "delivery_date")}
        rig.co.approve(rig.job.id, rig.job.run_id, echo)  # explicit simulated user decision
        for _ in range(5):
            rig.tick()
            if rig.engine().state == "COMPLETED":
                break
        assert rig.engine().state == "COMPLETED"
        assert [m.kind for m in rig.engine().outbound.values()] == ["rfq", "rfq", "counter", "booking"]
        assert all(q.source == "email" for q in rig.engine().ledger.quotes)
        assert len(calls.provider.asks) == 2
    finally:
        rig.close()
