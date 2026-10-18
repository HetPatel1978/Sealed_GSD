"""Transactional reply effects, recovery and separation from external outreach."""

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest

from sealed.core import checkpoint
from sealed.core.engine import Engine, SendResult
from sealed.core.events import EventLog
from sealed.core.models import Dealer, Inbound, Job
from sealed.ingest.deliver import DeliveryError, DeliveryStore, QueueTransport, ReplyDelivery
from sealed.ingest.mail import Binding, match_message
from sealed.ingest.store import InboxStore
from sealed.parser.quote import Context, input_digest, validate
from sealed.parser.store import ParseStore
from tests.helpers import FakeChecker
from tests.test_engine import RecordingTransport
from tests.test_gmail_ingestion import OWNER, SELLER, decoded, wire
from tests.test_quote_parser import SPEC, TEXT, extraction

NOW = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
BINDING = Binding("job", "run", "seller", SELLER, "[SEALED-test_run]", T0.isoformat(),
                  "rfq_01", "<original@fo.example>", "555")


def engine():
    job = Job("job", "test", SPEC, "19104", "Buyer", "this week", "2026-11-30", 90,
        (Dealer("seller", "Seller", SELLER),), run_id="run")
    result = Engine(job, QueueTransport(), FakeChecker(), None, cc=OWNER, tag="SEALED-test_run",
                    log=EventLog(job.id, job.run_id, job.mode, T0))
    result.create(0)
    result.state, result.round = "RUNNING", 2
    return result


class Harness:
    def __init__(self, directory, *, register=True, mode="sim"):
        self.directory = directory
        self.inbox = InboxStore(directory / "inbox.sqlite3")
        self.cache = ParseStore(directory / "parses.sqlite3")
        self.store = DeliveryStore(directory / "engine.sqlite3")
        self.service = ReplyDelivery(self.store, self.inbox, self.cache, account=OWNER, bindings=[BINDING],
            model="test-model", checker=FakeChecker(), qualifier=None)
        if register:
            initial = engine()
            initial.job = replace(initial.job, mode=mode)
            initial.log.mode = mode
            for event in initial.log.events:
                event["mode"] = mode
            self.store.register(OWNER, initial)

    def stage(self, *, gmail_id="123456", received_at="2026-10-18T16:00:00+00:00", review=False, cached=True, **changes):
        mail = decoded(wire(text=TEXT), gmail_id=gmail_id, received_at=received_at, **changes)
        match = match_message(mail, [BINDING], OWNER)
        self.inbox.stage(OWNER, mail, match)
        if cached:
            context = Context(SPEC, "USD", received_at, NOW.date().isoformat(), same_spec_allowed=True)
            raw = extraction(spec_status="unknown", spec_evidence="") if review else extraction()
            result = validate(raw, mail.clean_text, context)
            self.cache.put(OWNER, gmail_id, input_digest(mail.clean_text, context, "test-model"), result)
        return mail

    def deliver(self, gmail_id="123456"):
        return self.service.deliver(gmail_id, job_id="job", run_id="run", now=NOW)

    def load(self):
        return self.store.load(OWNER, "job", "run", checker=FakeChecker(), qualifier=None)

    def close(self):
        self.store.close()
        self.cache.close()
        self.inbox.close()


@pytest.fixture
def harness(tmp_path):
    result = Harness(tmp_path)
    yield result
    result.close()


def test_authenticated_cached_reply_updates_real_ledger_events_and_acknowledges_once(harness):
    mail = harness.stage()
    result = harness.deliver()
    eng = harness.load()
    assert result.status == "applied" and result.acknowledged and not result.duplicate
    assert len(eng.ledger.quotes) == 1 and eng.ledger.latest_firm("seller").price_total == 95
    assert eng.log.of_type("reply.received")[-1]["data"]["evidence"]["ref"] == mail.evidence_ref
    assert eng.log.of_type("quote.verified")[-1]["data"]["evidence"]["ref"] == mail.evidence_ref
    assert eng.log.of_type("reply.received")[-1]["data"]["subject"] == mail.subject
    assert not eng.log.of_type("message.sent") and not eng.log.of_type("message.queued")
    assert harness.deliver().duplicate and len(harness.load().ledger.quotes) == 1


def test_nonfirm_reply_stages_one_guarded_threaded_clarification_without_claiming_send(harness):
    mail = harness.stage(review=True)
    first, second = harness.deliver(), harness.deliver()
    eng = harness.load()
    outbox = harness.store.outbox(OWNER, "job", "run")
    assert len(outbox) == len(eng.ledger.quotes) == len(first.queued) == 1
    assert second.duplicate and not eng.ledger.current_firm()
    assert outbox[0].kind == "firm_up" and outbox[0].reply_to_message_id == mail.message_id
    assert outbox[0].reply_thread_id == mail.thread_id and outbox[0].subject == mail.subject
    assert eng.guard.check(outbox[0]).passed and not eng.log.of_type("message.sent")
    assert len(eng.log.of_type("message.queued")) == 1


def test_reopen_restores_counters_evidence_and_outbox_without_second_action(tmp_path):
    h = Harness(tmp_path)
    h.stage(review=True)
    h.deliver()
    original = checkpoint.dump(h.load())
    h.close()
    reopened = Harness(tmp_path, register=False)
    try:
        assert reopened.deliver().duplicate
        assert json.loads(checkpoint.dump(reopened.load())) == json.loads(original)
        assert len(reopened.store.outbox(OWNER, "job", "run")) == 1
    finally:
        reopened.close()


def test_crash_after_commit_before_acknowledgment_replays_receipt_only(harness, monkeypatch):
    harness.stage(review=True)
    real_ack = harness.inbox.acknowledge
    monkeypatch.setattr(harness.inbox, "acknowledge", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        harness.deliver()
    assert harness.inbox.get(OWNER, "123456")[0] == "ready"
    assert len(harness.load().ledger.quotes) == 1
    monkeypatch.setattr(harness.inbox, "acknowledge", real_ack)
    result = harness.deliver()
    assert result.duplicate and result.acknowledged
    assert len(harness.load().ledger.quotes) == len(harness.store.outbox(OWNER, "job", "run")) == 1


def test_failure_before_commit_rolls_back_quote_events_outbox_and_leaves_inbox_ready(harness, monkeypatch):
    harness.stage(review=True)
    real_save = harness.store._save
    def fail(account, engine):
        real_save(account, engine)
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(harness.store, "_save", fail)
    with pytest.raises(sqlite3.OperationalError):
        harness.deliver()
    assert not harness.load().ledger.quotes and not harness.store.outbox(OWNER, "job", "run")
    assert harness.inbox.get(OWNER, "123456")[0] == "ready"
    assert harness.store._receipt(OWNER, "123456") is None
    monkeypatch.setattr(harness.store, "_save", real_save)
    assert harness.deliver().acknowledged


def test_missing_parse_pauses_approval_without_consuming_or_repeating_review_event(harness):
    harness.stage(cached=False)
    result = harness.deliver()
    assert result.status == "waiting_parse" and not result.acknowledged
    eng = harness.load()
    assert eng.state == "NEEDS_HUMAN" and eng.proposal is None
    assert not eng.ledger.quotes and not harness.store.outbox(OWNER, "job", "run")
    count = len(eng.log.events)
    assert harness.deliver().status == "waiting_parse" and len(harness.load().log.events) == count


def test_unverified_or_other_scope_mail_never_reaches_ledger(harness):
    harness.stage(authenticated=False, flags=("sender_auth_unverified",))
    with pytest.raises(DeliveryError, match="reply_not_ready"):
        harness.deliver()
    assert not harness.load().ledger.quotes


def test_delivery_conflict_cannot_apply_one_gmail_message_to_another_run(harness):
    harness.stage()
    harness.deliver()
    with pytest.raises(DeliveryError, match="reply_delivery_conflict"):
        harness.service.deliver("123456", job_id="job", run_id="other", now=NOW)


def test_checkpoint_cannot_be_loaded_under_another_database_scope(harness):
    harness.store.db.execute("UPDATE engine_runs SET run_id='other'")
    with pytest.raises(DeliveryError, match="invalid_run_checkpoint"):
        harness.store.load(OWNER, "job", "other", checker=FakeChecker(), qualifier=None)


def test_late_old_nonfirm_reply_does_not_replace_new_quote_or_trigger_clarification(harness):
    harness.stage(gmail_id="200000")
    harness.deliver("200000")
    harness.stage(gmail_id="100000", received_at="2026-10-18T15:00:00+00:00", review=True)
    harness.deliver("100000")
    eng = harness.load()
    assert len(eng.ledger.quotes) == 2 and eng.ledger.latest_firm("seller").mail_evidence.gmail_id == "200000"
    assert not harness.store.outbox(OWNER, "job", "run")
    assert eng.reply_context["seller"]["evidence"].gmail_id == "200000"


def test_same_second_receipts_use_gmail_id_to_resolve_late_old_messages(harness):
    harness.stage(gmail_id="200000")
    harness.deliver("200000")
    harness.stage(gmail_id="100000", review=True)
    harness.deliver("100000")
    assert harness.load().ledger.latest_firm("seller").mail_evidence.gmail_id == "200000"
    assert not harness.store.outbox(OWNER, "job", "run")


def test_dispatch_requires_authorization_and_records_actual_result_once(harness):
    harness.stage(review=True)
    msg_id = harness.deliver().queued[0]
    transport = RecordingTransport()
    with pytest.raises(DeliveryError, match="send_authorization_required"):
        harness.service.dispatch(msg_id, job_id="job", run_id="run", now=301, transport=transport)
    assert not transport.sent
    assert harness.service.dispatch(msg_id, job_id="job", run_id="run", now=301, transport=transport, authorized=True) == "sent"
    assert len(transport.sent) == len(harness.load().log.of_type("message.sent")) == 1
    assert not harness.load().queued_messages
    with pytest.raises(DeliveryError, match="outbox_not_pending_do_not_resend"):
        harness.service.dispatch(msg_id, job_id="job", run_id="run", now=302, transport=transport, authorized=True)
    assert len(transport.sent) == 1


def test_crash_during_external_send_retains_inflight_marker_and_forbids_retry(harness):
    harness.stage(review=True)
    msg_id = harness.deliver().queued[0]
    class Crash(BaseException):
        pass
    class Transport:
        def send(self, *args):
            raise Crash()
    with pytest.raises(Crash):
        harness.service.dispatch(msg_id, job_id="job", run_id="run", now=301, transport=Transport(), authorized=True)
    assert harness.store.in_flight(OWNER, "job", "run")
    with pytest.raises(DeliveryError, match="outbox_not_pending_do_not_resend"):
        harness.service.dispatch(msg_id, job_id="job", run_id="run", now=302, transport=RecordingTransport(), authorized=True)
    harness.stage(gmail_id="999999")
    with pytest.raises(DeliveryError, match="outreach_in_progress"):
        harness.deliver("999999")


def test_transport_error_is_sanitized_and_pauses_without_retry(harness):
    harness.stage(review=True)
    msg_id = harness.deliver().queued[0]
    class Failure:
        def send(self, *args):
            raise RuntimeError("Authorization Bearer PRIVATE_TOKEN")
    assert harness.service.dispatch(msg_id, job_id="job", run_id="run", now=301, transport=Failure(), authorized=True) == "review"
    assert harness.load().state == "NEEDS_HUMAN"
    assert "PRIVATE_TOKEN" not in checkpoint.dump(harness.load())


def test_new_firm_reply_suppresses_obsolete_queued_clarification(harness):
    harness.stage(review=True)
    msg_id = harness.deliver().queued[0]
    harness.stage(gmail_id="999999", received_at="2026-10-18T16:30:00+00:00")
    harness.deliver("999999")
    transport = RecordingTransport()
    assert harness.service.dispatch(msg_id, job_id="job", run_id="run", now=301, transport=transport, authorized=True) == "suppressed"
    assert not transport.sent and not harness.store.outbox(OWNER, "job", "run")
    assert harness.load().state == "RUNNING"


def test_two_workers_deliver_one_reply_once(tmp_path, harness):
    from concurrent.futures import ThreadPoolExecutor
    harness.stage(review=True)
    def worker():
        h = Harness(tmp_path, register=False)
        try:
            return h.deliver()
        finally:
            h.close()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: worker(), range(2)))
    assert sorted(r.duplicate for r in results) == [False, True]
    assert len(harness.load().ledger.quotes) == len(harness.store.outbox(OWNER, "job", "run")) == 1


def test_failed_post_send_commit_retains_claim_and_does_not_retry_actual_send(harness, monkeypatch):
    harness.stage(review=True)
    msg_id = harness.deliver().queued[0]
    transport = RecordingTransport()
    monkeypatch.setattr(harness.store, "_save", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    with pytest.raises(sqlite3.OperationalError):
        harness.service.dispatch(msg_id, job_id="job", run_id="run", now=301, transport=transport, authorized=True)
    assert len(transport.sent) == 1 and harness.store.in_flight(OWNER, "job", "run")
    assert not harness.load().log.of_type("message.sent")
    with pytest.raises(DeliveryError, match="outbox_not_pending_do_not_resend"):
        harness.service.dispatch(msg_id, job_id="job", run_id="run", now=302, transport=transport, authorized=True)
    assert len(transport.sent) == 1


def test_new_unparsed_terms_clear_a_pending_approval(harness):
    from sealed.core.engine import StaleProposal
    harness.stage()
    harness.deliver()
    eng = harness.load()
    quote = eng.ledger.latest_firm("seller")
    proposal = {"proposal_id": "p_01", "vendor_id": "seller", "quote_id": quote.quote_id,
        "price_total": 95.0, "currency": "USD", "delivery_date": "2026-10-25", "flags": []}
    eng.state, eng.proposal = "AWAITING_APPROVAL", proposal
    with harness.store.transaction():
        harness.store._save(OWNER, eng)
    harness.stage(gmail_id="999999", received_at="2026-10-18T16:30:00+00:00", cached=False)
    assert harness.deliver("999999").status == "waiting_parse"
    restored = harness.load()
    assert restored.proposal is None
    with pytest.raises(StaleProposal):
        restored.approve(301, proposal)


def test_live_dispatch_without_real_gmail_audit_pauses_even_if_transport_reports_sent(tmp_path):
    harness = Harness(tmp_path, mode="live")
    try:
        harness.stage(review=True)
        msg_id = harness.deliver().queued[0]
        assert harness.service.dispatch(msg_id, job_id="job", run_id="run", now=301,
            transport=RecordingTransport(), authorized=True) == "review"
        assert harness.load().state == "NEEDS_HUMAN"
        assert harness.store.trusted_bindings(OWNER, "job", "run") == []
    finally:
        harness.close()


def test_passed_live_send_audit_persists_real_reply_binding_with_outbox_result(tmp_path):
    from sealed.core.models import EmailAudit, MailEvidence, outbound_fingerprint
    h = Harness(tmp_path, mode="live")
    try:
        h.stage(review=True)
        msg_id = h.deliver().queued[0]
        class Audited(RecordingTransport):
            def send(self, now, msg, approve_preview):
                result = super().send(now, msg, approve_preview)
                result.audit = EmailAudit(msg.job_id, msg.run_id, msg.message_id, outbound_fingerprint(msg),
                    "passed", None, "brief_constraints", True, True, (), (),
                    MailEvidence("888888", "555", "<sent@fo.example>", "2026-10-18T17:01:00+00:00",
                        "pass", "pass", "pass", "gmail_dmarc"))
                return result
        assert h.service.dispatch(msg_id, job_id="job", run_id="run", now=301, transport=Audited(), authorized=True) == "sent"
        bindings = h.store.trusted_bindings(OWNER, "job", "run")
        assert len(bindings) == 1 and bindings[0].outbound_id == msg_id
        assert bindings[0].outbound_message_id == "<sent@fo.example>" and bindings[0].thread_id == "555"
        assert len(h.load().log.of_type("audit.checked")) == 1
    finally:
        h.close()


def test_batch_handoff_drains_matching_replies_and_replay_does_nothing(harness):
    harness.stage(gmail_id="100000")
    harness.stage(gmail_id="200000", received_at="2026-10-18T16:30:00+00:00", review=True)
    results = harness.service.deliver_pending(job_id="job", run_id="run", now=NOW)
    assert len(results) == 2 and all(result.acknowledged for result in results)
    assert len(harness.load().ledger.quotes) == 2
    assert not harness.load().ledger.current_firm()
    assert len(harness.store.outbox(OWNER, "job", "run")) == 1
    assert harness.service.deliver_pending(job_id="job", run_id="run", now=NOW) == []


def test_queued_booking_does_not_decline_other_sellers_until_actual_checked_send(harness):
    harness.stage()
    harness.deliver()
    eng = harness.load()
    eng.job = replace(eng.job, dealers=eng.job.dealers + (Dealer("other", "Other", "other@example.org"),))
    eng.ledger.dealer_emails["other"] = "other@example.org"
    eng.contacted.add("other")
    quote = eng.ledger.latest_firm("seller")
    eng.proposal = {"proposal_id": "p_01", "vendor_id": "seller", "quote_id": quote.quote_id,
        "price_total": 95.0, "currency": "USD", "delivery_date": "2026-10-25", "flags": []}
    eng.state = "AWAITING_APPROVAL"
    eng.approve(301, eng.proposal.copy())
    assert eng.booking_message is None and len(eng.queued_messages) == 1
    booking = eng.outbound[next(iter(eng.queued_messages))]
    assert booking.kind == "booking" and not any(m.kind == "cancel" for m in eng.outbound.values())
    result = RecordingTransport().send(302, booking, lambda preview: eng.guard.check(booking, preview=preview).passed)
    eng.on_send_result(302, booking.message_id, result)
    assert eng.booking_message == booking.message_id
    assert len(eng.queued_messages) == 1 and eng.outbound[next(iter(eng.queued_messages))].kind == "cancel"


def test_checkpoint_roundtrip_excludes_runtime_private_callbacks_and_rejects_unknown_fields():
    eng = engine()
    class PrivateChecker:
        sentinel = "PRIVATE_CEILING_VALUE"
        def contains_secret(self, text):
            return False
    eng.guard._checker = PrivateChecker()
    eng.qualifier = lambda *args: "PRIVATE_CEILING_VALUE"
    data = checkpoint.dump(eng)
    assert "PRIVATE_CEILING_VALUE" not in data and "checker" not in data and "qualifier" not in data
    restored = checkpoint.load(data, transport=QueueTransport(), checker=FakeChecker(), qualifier=None)
    assert json.loads(checkpoint.dump(restored)) == json.loads(data)
    with pytest.raises(ValueError):
        checkpoint.load(json.dumps({**json.loads(data), "ceiling": 12345}), transport=QueueTransport(), checker=FakeChecker(), qualifier=None)
