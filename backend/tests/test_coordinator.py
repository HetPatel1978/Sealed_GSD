"""Full account cycles and safety boundaries with offline integrations."""

from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest

from sealed.core.engine import StaleProposal, Waits
from sealed.core.models import Dealer, Job
from sealed.ingest.deliver import DeliveryError, DeliveryStore
from sealed.ingest.gmail import GmailError
from sealed.ingest.store import InboxStore
from sealed.parser.quote import ParserError
from sealed.parser.store import ParseStore
from sealed.private import vault
from sealed.private.runtime import for_run
from sealed.service.coordinator import Coordinator, Policy, Runtime
from sealed.private.runtime import run_coordinator_demo
from sealed.service.lock import worker_lock
from sealed.service.sim_demo import Clock, Extractor, Mailbox, OWNER, SELLER, SPEC, Transport


class Rig:
    def __init__(self, path, *, policy=Policy(True, True), register=True):
        self.path = path
        self.clock, self.reader, self.extractor = Clock(), Mailbox(), Extractor()
        self.transport = Transport(self.reader, self.clock)
        self.store = DeliveryStore(path / "engine.sqlite3")
        self.inbox = InboxStore(path / "inbox.sqlite3")
        self.cache = ParseStore(path / "parse.sqlite3")
        self.job = Job("coordinator-test", "test", SPEC, "19104", "Buyer", "this week", "2026-10-25", 90,
            (Dealer("seller", "Synthetic seller", SELLER),), run_id="run", mode="sim")
        vault.store(self.job.id, 150)
        self.co = Coordinator(self.store, self.inbox, self.cache, account=OWNER, reader=self.reader,
            extractor=self.extractor, runtime=for_run, transport_factory=lambda eng: self.transport,
            policy=policy, clock=self.clock)
        if register:
            self.co.register(self.job, cc=OWNER, tag="SEALED-COORDINATOR-TEST",
                waits=Waits(nudge_after=1, round1_close=2, round2_call_after=1, round2_close=4))

    def engine(self):
        runtime = for_run(self.job.id, self.job.run_id)
        return self.store.load(OWNER, self.job.id, self.job.run_id, checker=runtime.checker, qualifier=runtime.qualifier)

    def tick(self):
        report = self.co.tick()
        self.clock.advance()
        return report

    def until_approval(self):
        for _ in range(10):
            self.tick()
            if self.engine().state == "AWAITING_APPROVAL":
                return self.engine().proposal.copy()
        pytest.fail("No proposal")

    def close(self):
        self.store.close()
        self.inbox.close()
        self.cache.close()
        vault.forget(self.job.id)


@pytest.fixture
def rig(tmp_path):
    result = Rig(tmp_path)
    yield result
    result.close()


def test_complete_offline_run_exercises_real_coordinator_and_restart():
    report = run_coordinator_demo()
    assert report["state"] == "COMPLETED" and report["final_price"] == 90
    assert report["outreach"] == ["rfq", "counter", "booking"]
    assert report["approval_source"] == "explicit_simulated_user" and report["restart_replay_safe"]
    assert report["timers_fired"] == 4 and report["live_network_requests"] == report["live_messages_sent"] == 0


def test_mail_is_delivered_before_round_timer_and_only_once(rig):
    for _ in range(5):
        rig.tick()
    events = rig.engine().log.events
    quote = next(e for e in events if e["type"] == "quote.parsed")
    decision = next(e for e in events if e["type"] == "strategy.decided" and e["data"]["action"] == "counter")
    assert quote["id"] < decision["id"]
    assert len(rig.engine().ledger.quotes) == 2
    before = (rig.extractor.calls, len(rig.transport.sent), len(events))
    rig.co.tick()
    assert before == (rig.extractor.calls, len(rig.transport.sent), len(rig.engine().log.events))


def test_approval_gate_waits_for_explicit_exact_echo(rig):
    proposal = rig.until_approval()
    for _ in range(3):
        rig.tick()
    assert not any(msg.kind == "booking" for msg in rig.transport.sent)
    with pytest.raises(StaleProposal):
        rig.co.approve(rig.job.id, rig.job.run_id, {**proposal, "price_total": 89})
    rig.co.approve(rig.job.id, rig.job.run_id, proposal)
    assert rig.engine().state == "BOOKING" and not rig.engine().log.of_type("job.completed")
    for _ in range(3):
        rig.tick()
    assert rig.engine().state == "COMPLETED"


def test_default_policy_does_not_start_or_dispatch_unapproved_job(tmp_path):
    r = Rig(tmp_path, policy=Policy())
    try:
        r.tick()
        assert r.engine().state == "CREATED" and not r.transport.sent and not r.extractor.calls
        assert not r.store.outbox(OWNER, r.job.id, r.job.run_id)
    finally:
        r.close()


def test_unapproved_model_request_blocks_timers_and_dispatch(rig):
    rig.tick()  # start
    rig.tick()  # RFQ -> synthetic reply
    rig.co.policy = Policy(False, True)
    timer_count = rig.store.db.execute("SELECT COUNT(*) FROM coordinator_timers").fetchone()[0]
    report = rig.tick()
    assert report["runs"][0]["status"] == "model_authorization_required"
    assert rig.extractor.calls == 0 and len(rig.transport.sent) == 1
    assert rig.store.db.execute("SELECT COUNT(*) FROM coordinator_timers").fetchone()[0] == timer_count


def test_poll_failure_does_not_dispatch_previously_queued_message(rig, monkeypatch):
    rig.tick()
    # Initial RFQ has no binding yet; supply an existing binding to force a poll.
    from sealed.ingest.mail import Binding
    rig.co.bindings = [Binding(rig.job.id, rig.job.run_id, "seller", SELLER, "[SEALED-COORDINATOR-TEST]",
        rig.clock().isoformat(), "earlier")]
    monkeypatch.setattr(rig.reader, "search", lambda query: (_ for _ in ()).throw(GmailError("mail down")))
    assert rig.tick()["status"] == "mail_poll_failed"
    assert not rig.transport.sent


def test_parser_failure_pauses_once_and_never_exports_again_automatically(rig, monkeypatch):
    rig.tick()
    rig.tick()
    calls = []
    def fail(text, context):
        calls.append(text)
        raise ParserError("parser_timeout")
    monkeypatch.setattr(rig.extractor, "parse", fail)
    rig.tick()
    assert rig.engine().state == "NEEDS_HUMAN" and len(calls) == 1
    rig.tick()
    assert len(calls) == 1 and len(rig.transport.sent) == 1
    assert len(rig.inbox.pending(OWNER)) == 1


def test_private_runtime_missing_after_restart_prevents_actions_until_restored(rig):
    rig.tick()
    vault.forget(rig.job.id)
    assert rig.tick()["runs"][0]["status"] == "private_runtime_required"
    assert not rig.transport.sent
    vault.store(rig.job.id, 150)
    rig.tick()
    assert len(rig.transport.sent) == 1


def test_timers_are_durable_and_cannot_fire_twice(rig):
    rig.until_approval()
    before = list(rig.store.db.execute("SELECT name FROM coordinator_timers ORDER BY rowid"))
    assert len(before) == len(set(before)) == 4
    for _ in range(5):
        rig.tick()
    assert before == list(rig.store.db.execute("SELECT name FROM coordinator_timers ORDER BY rowid"))


def test_pending_counter_must_settle_before_overdue_round_close(rig):
    for _ in range(3):
        rig.tick()
    assert rig.store.outbox(OWNER, rig.job.id, rig.job.run_id)[0].kind == "counter"
    rig.co.policy = Policy(True, False)
    rig.clock.advance(100)
    assert rig.tick()["runs"][0]["status"] == "dispatch_authorization_required"
    assert rig.engine().state == "RUNNING" and rig.engine().proposal is None


def test_cancel_cancels_pending_outreach_without_contacting_anyone(rig):
    rig.tick()
    rig.co.cancel(rig.job.id, rig.job.run_id)
    rig.tick()
    assert rig.engine().state == "CANCELLED" and rig.engine().proposal is None
    assert not rig.transport.sent and not rig.store.outbox(OWNER, rig.job.id, rig.job.run_id)


def test_any_uncertain_account_claim_stops_other_actions_and_timers(rig):
    rig.tick()
    rig.store.db.execute("UPDATE engine_outbox SET status='dispatching'")
    before = rig.store.db.execute("SELECT COUNT(*) FROM coordinator_timers").fetchone()[0]
    rig.clock.advance(100)
    assert rig.tick()["status"] == "outreach_requires_reconciliation"
    assert not rig.transport.sent
    assert rig.store.db.execute("SELECT COUNT(*) FROM coordinator_timers").fetchone()[0] == before


def test_reject_requires_current_proposal_and_does_not_book(rig):
    proposal = rig.until_approval()
    rig.co.reject(rig.job.id, rig.job.run_id, proposal["proposal_id"], "test rejection")
    assert rig.engine().state == "WAITING_AFTER_REJECT"
    rig.tick()
    assert not any(msg.kind == "booking" for msg in rig.transport.sent)


def test_process_worker_lock_refuses_second_worker_and_releases_cleanly(tmp_path):
    path = tmp_path / "worker.lock"
    with worker_lock(path):
        with pytest.raises(RuntimeError, match="coordinator_already_running"):
            with worker_lock(path):
                pytest.fail("Duplicate worker entered")
    with worker_lock(path):
        pass


def test_run_loop_honors_stop_and_cycle_limit(rig):
    stop = Event()
    reports = []
    def observe(report):
        reports.append(report)
        stop.set()
    assert rig.co.run(stop=stop, interval=0.1, max_cycles=10, on_tick=observe) == 1
    assert len(reports) == 1
