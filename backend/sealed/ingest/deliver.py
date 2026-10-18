"""Durable cached-parser -> engine handoff and a guarded transactional outbox.

No model or Fo client is constructed here. The caller supplies trusted bindings,
runtime private-zone callbacks, and (only for explicitly authorized dispatch) a
transport. A receipt, public checkpoint, events and intents commit together;
the separately staged inbox is acknowledged afterward and can safely be replayed.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import TypeAdapter

from sealed.core import checkpoint
from sealed.core.engine import SendResult
from sealed.core.models import Inbound, MailEvidence, Outbound, outbound_fingerprint
from sealed.ingest.mail import Binding, MailMessage, addresses, match_message
from sealed.parser.quote import Context, input_digest

MAIL = TypeAdapter(MailMessage)
OUTBOUND = TypeAdapter(Outbound)


def parsing_context(engine, mail, match, now):
    """Shared trusted context for extraction and delivery, including booking linkage."""
    sent = {e["data"]["message_id"] for e in engine.log.of_type("message.sent")}
    linked = engine.outbound.get(match.in_reply_to or "")
    booking = bool(linked and linked.kind == "booking" and linked.message_id in sent
        and linked.message_id == engine.booking_message and linked.dealer_id == match.binding.dealer_id)
    return Context(engine.job.details, engine.job.currency, mail.received_at, now.date().isoformat(),
                   same_spec_allowed=True, booking_requested=booking)


class DeliveryError(Exception):
    pass


class QueueTransport:
    """Capture intents only. It never approves a preview or reports a send."""

    def send(self, now, msg, approve_preview):
        return SendResult(False, now, queued=True)


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    gmail_id: str
    duplicate: bool
    acknowledged: bool
    quote_id: str | None
    queued: tuple[str, ...]


def _json(value):
    def encode(item):
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        raise TypeError("Unsupported durable value.")
    return json.dumps(value, sort_keys=True, allow_nan=False, default=encode)


class DeliveryStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS engine_runs (
                account TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                revision INTEGER NOT NULL, checkpoint TEXT NOT NULL,
                PRIMARY KEY(account, job_id, run_id));
            CREATE TABLE IF NOT EXISTS engine_events (
                account TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                event_id INTEGER NOT NULL, event TEXT NOT NULL,
                PRIMARY KEY(account, job_id, run_id, event_id));
            CREATE TABLE IF NOT EXISTS reply_deliveries (
                account TEXT NOT NULL, gmail_id TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                source_digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(account, gmail_id));
            CREATE TABLE IF NOT EXISTS engine_outbox (
                account TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                message_id TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT,
                PRIMARY KEY(account, job_id, run_id, message_id));
            CREATE TABLE IF NOT EXISTS engine_mail_bindings (
                account TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                message_id TEXT NOT NULL, binding TEXT NOT NULL,
                PRIMARY KEY(account, job_id, run_id, message_id));
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def register(self, account, engine):
        """Register an existing public run; never overwrite its later checkpoint."""
        account = account.casefold()
        with self.transaction():
            key = (account, engine.job.id, engine.job.run_id)
            if self.db.execute("SELECT 1 FROM engine_runs WHERE account=? AND job_id=? AND run_id=?", key).fetchone():
                raise DeliveryError("run_already_registered")
            self.db.execute("INSERT INTO engine_runs VALUES (?,?,?,?,?)", (*key, 0, checkpoint.dump(engine)))
            self._save(account, engine)

    def load(self, account, job_id, run_id, *, checker, qualifier):
        row = self.db.execute("SELECT checkpoint FROM engine_runs WHERE account=? AND job_id=? AND run_id=?",
                             (account.casefold(), job_id, run_id)).fetchone()
        if row is None:
            raise DeliveryError("run_not_registered")
        try:
            engine = checkpoint.load(row[0], transport=QueueTransport(), checker=checker, qualifier=qualifier)
            if (engine.job.id, engine.job.run_id) != (job_id, run_id):
                raise ValueError("Checkpoint scope mismatch.")
            return engine
        except (ValueError, TypeError, KeyError):
            raise DeliveryError("invalid_run_checkpoint") from None

    def _save(self, account, engine):
        key = (account, engine.job.id, engine.job.run_id)
        count = self.db.execute("SELECT COUNT(*) FROM engine_events WHERE account=? AND job_id=? AND run_id=?", key).fetchone()[0]
        for event in engine.log.events[count:]:
            self.db.execute("INSERT INTO engine_events VALUES (?,?,?,?,?)", (*key, event["id"], _json(event)))
        for message_id in sorted(engine.queued_messages):
            msg = engine.outbound[message_id]
            payload, fingerprint = _json(asdict(msg)), outbound_fingerprint(msg)
            prior = self.db.execute("SELECT fingerprint, status FROM engine_outbox WHERE account=? AND job_id=? AND run_id=? AND message_id=?",
                                    (*key, message_id)).fetchone()
            if prior is None:
                self.db.execute("INSERT INTO engine_outbox VALUES (?,?,?,?,?,?,?,NULL)", (*key, message_id, fingerprint, payload, "pending"))
            elif prior[0] != fingerprint:
                raise DeliveryError("outbox_payload_conflict")
        self.db.execute("UPDATE engine_runs SET revision=revision+1, checkpoint=? WHERE account=? AND job_id=? AND run_id=?",
                        (checkpoint.dump(engine), *key))

    def outbox(self, account, job_id, run_id, *, status="pending"):
        return [OUTBOUND.validate_json(row[0]) for row in self.db.execute(
            "SELECT payload FROM engine_outbox WHERE account=? AND job_id=? AND run_id=? AND status=? ORDER BY rowid",
            (account.casefold(), job_id, run_id, status))]

    def in_flight(self, account, job_id, run_id):
        return bool(self.db.execute("SELECT 1 FROM engine_outbox WHERE account=? AND job_id=? AND run_id=? AND status='dispatching'",
                                   (account.casefold(), job_id, run_id)).fetchone())

    def trusted_bindings(self, account, job_id, run_id):
        return [Binding(**json.loads(row[0])) for row in self.db.execute(
            "SELECT binding FROM engine_mail_bindings WHERE account=? AND job_id=? AND run_id=? ORDER BY rowid",
            (account.casefold(), job_id, run_id))]

    def _receipt(self, account, gmail_id):
        row = self.db.execute("SELECT job_id,run_id,source_digest,status,result FROM reply_deliveries WHERE account=? AND gmail_id=?",
                              (account, gmail_id)).fetchone()
        return row

    def _record(self, account, engine, gmail_id, digest, status, result):
        self.db.execute("INSERT INTO reply_deliveries VALUES (?,?,?,?,?,?,?) ON CONFLICT(account,gmail_id) DO UPDATE SET status=excluded.status,result=excluded.result",
            (account, gmail_id, engine.job.id, engine.job.run_id, digest, status, _json(result)))
        self._save(account, engine)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ReplyDelivery:
    def __init__(self, store, inbox, cache, *, account, bindings, model, checker, qualifier):
        self.store, self.inbox, self.cache = store, inbox, cache
        self.account, self.bindings, self.model = account.casefold(), list(bindings), model
        self.checker, self.qualifier = checker, qualifier

    def _load(self, job_id, run_id):
        return self.store.load(self.account, job_id, run_id, checker=self.checker, qualifier=self.qualifier)

    def _block(self, engine, now, code):
        engine.state, engine.proposal = "NEEDS_HUMAN", None
        engine.log.emit(now, "job.needs_human", None, {"reason_code": code, "blocking": True,
            "message": "A newer reply could not be safely applied. Outreach and approval are paused; the inbox item remains pending."})

    def deliver(self, gmail_id: str, *, job_id: str, run_id: str, now: datetime) -> DeliveryResult:
        if now.tzinfo is None:
            raise DeliveryError("timezone_required")
        staged = self.inbox.get(self.account, gmail_id)
        if staged is None:
            raise DeliveryError("reply_not_staged")
        status, record = staged
        try:
            mail = MAIL.validate_json(_json(record["message"]), strict=True)
        except (ValueError, TypeError, KeyError):
            raise DeliveryError("invalid_staged_evidence") from None
        if mail.gmail_id != gmail_id:
            raise DeliveryError("evidence_identity_mismatch")
        if not mail.gmail_id.isdigit() or not mail.thread_id.isdigit() or not mail.message_id:
            raise DeliveryError("invalid_staged_evidence")
        digest = hashlib.sha256(_json(record["message"]).encode()).hexdigest()
        duplicate = False
        with self.store.transaction():
            prior = self.store._receipt(self.account, gmail_id)
            if prior and prior[:3] != (job_id, run_id, digest):
                raise DeliveryError("reply_delivery_conflict")
            if prior and prior[3] == "applied":
                result, duplicate = json.loads(prior[4]), True
            else:
                if status != "ready":
                    raise DeliveryError("reply_not_ready")
                if self.store.in_flight(self.account, job_id, run_id):
                    raise DeliveryError("outreach_in_progress")
                engine = self._load(job_id, run_id)
                bindings = self.bindings + self.store.trusted_bindings(self.account, job_id, run_id)
                match = match_message(mail, bindings, self.account)
                if (match.status != "ready" or not match.binding
                        or match.binding.scope[:2] != (job_id, run_id)
                        or match.binding.tag != f"[{engine.tag}]"):
                    raise DeliveryError("reply_scope_or_identity_mismatch")
                try:
                    dealer = engine.job.dealer(match.binding.dealer_id)
                except StopIteration:
                    raise DeliveryError("dealer_identity_not_configured") from None
                if not engine.email_to(dealer.id) or engine.email_to(dealer.id).casefold() != mail.from_addr:
                    raise DeliveryError("dealer_identity_not_configured")
                time = (now - engine.log.t0).total_seconds() / 60
                source_time = (datetime.fromisoformat(mail.received_at) - engine.log.t0).total_seconds() / 60
                context = parsing_context(engine, mail, match, now)
                parsed = self.cache.get(self.account, gmail_id, input_digest(mail.clean_text, context, self.model))
                if parsed is None:
                    if not prior:
                        self._block(engine, time, "parse_result_missing")
                        result = {"quote_id": None, "queued": []}
                        self.store._record(self.account, engine, gmail_id, digest, "waiting_parse", result)
                    return DeliveryResult("waiting_parse", gmail_id, bool(prior), False, None, ())
                evidence = MailEvidence(mail.gmail_id, mail.thread_id, mail.message_id, mail.received_at,
                    mail.spf, mail.dkim, mail.dmarc, "gmail_dmarc")
                inbound = Inbound(dealer.id, "email", mail.clean_text, parsed.parsed, in_reply_to=match.in_reply_to,
                    from_addr=mail.from_addr, mail_evidence=evidence, subject=mail.subject)
                before = set(engine.queued_messages)
                old_count = len(engine.ledger.quotes)
                try:
                    engine.on_inbound(time, inbound, received_at=source_time)
                except Exception:
                    engine = self._load(job_id, run_id)
                    self._block(engine, time, "engine_delivery_failed")
                    result = {"quote_id": None, "queued": []}
                    self.store._record(self.account, engine, gmail_id, digest, "waiting_engine", result)
                    return DeliveryResult("waiting_engine", gmail_id, False, False, None, ())
                result = {"quote_id": engine.ledger.quotes[-1].quote_id if len(engine.ledger.quotes) > old_count else None,
                          "queued": sorted(engine.queued_messages - before)}
                self.store._record(self.account, engine, gmail_id, digest, "applied", result)
        # Commit precedes acknowledgment. A crash here reuses the committed receipt.
        self.inbox.acknowledge(self.account, gmail_id)
        staged = self.inbox.get(self.account, gmail_id)
        acknowledged = bool(staged and staged[0] == "consumed")
        return DeliveryResult("applied", gmail_id, duplicate, acknowledged, result["quote_id"], tuple(result["queued"]))

    def deliver_pending(self, *, job_id, run_id, now):
        pending = self.inbox.pending(self.account)
        pending.sort(key=lambda record: (record["message"]["received_at"], int(record["message"]["gmail_id"])))
        results = []
        for record in pending:
            binding = record["match"].get("binding")
            if binding and (binding["job_id"], binding["run_id"]) == (job_id, run_id):
                results.append(self.deliver(record["message"]["gmail_id"], job_id=job_id, run_id=run_id, now=now))
        return results

    def dispatch(self, message_id, *, job_id, run_id, now, transport, authorized=False):
        """One authorized attempt. A dispatching record is never automatically retried."""
        if not authorized:
            raise DeliveryError("send_authorization_required")
        key = (self.account, job_id, run_id, message_id)
        with self.store.transaction():
            engine = self._load(job_id, run_id)
            row = self.store.db.execute("SELECT status FROM engine_outbox WHERE account=? AND job_id=? AND run_id=? AND message_id=?", key).fetchone()
            if not row or row[0] != "pending":
                raise DeliveryError("outbox_not_pending_do_not_resend")
            if engine.state not in {"RUNNING", "BOOKING", "AWAITING_APPROVAL", "WAITING_AFTER_REJECT"}:
                raise DeliveryError("job_paused")
            if self.store.db.execute("SELECT 1 FROM engine_outbox WHERE account=? AND status='dispatching'", (self.account,)).fetchone():
                raise DeliveryError("outreach_in_progress")
            msg = engine.outbound[message_id]
            # Recheck current ledger authority before dispatching any queued prices.
            stale = False
            for _, source, quote_id in msg.price_sources:
                if source == "ledger":
                    quote = engine.ledger.get(quote_id)
                    latest = engine.ledger.latest_firm(quote.dealer_id)
                    stale |= latest is None or latest.quote_id != quote_id
                    valid_until = (quote.breakdown or {}).get("valid_until")
                    stale |= bool(valid_until and valid_until < (engine.log.t0 + timedelta(minutes=now)).date().isoformat())
            if msg.kind == "firm_up":
                stale |= engine.ledger.latest_firm(msg.dealer_id) is not None or engine.ledger.declined(msg.dealer_id)
            guard_passed = engine.guard.check(msg).passed
            if stale or not guard_passed:
                self.store.db.execute("UPDATE engine_outbox SET status='suppressed' WHERE account=? AND job_id=? AND run_id=? AND message_id=?", key)
                engine.queued_messages.discard(message_id)
                engine.log.emit(now, "message.suppressed", msg.dealer_id, {"message_id": message_id,
                    "reason_code": "terms_superseded" if stale else "guard_blocked"})
                if msg.kind == "booking" or not guard_passed:
                    self._block(engine, now, "queued_outreach_needs_review")
                self.store._save(self.account, engine)
                return "suppressed"
            self.store.db.execute("UPDATE engine_outbox SET status='dispatching' WHERE account=? AND job_id=? AND run_id=? AND message_id=?", key)
        def approve(preview):
            checked = engine.guard.check(msg, preview=preview)
            engine._guard_event(now, msg, checked)
            return checked.passed
        try:
            result = transport.send(now, msg, approve)
        except Exception:
            result = SendResult(False, now, reason_code="send_unclear")
        if engine.job.mode == "live" and msg.channel == "email" and result.sent and (result.audit is None or not result.audit.matched):
            result.reason_code = result.reason_code or "audit_mismatch"
        with self.store.transaction():
            engine.on_send_result(now, message_id, result)
            if result.sent and result.reason_code is None and result.audit and result.audit.matched and result.audit.evidence:
                evidence = result.audit.evidence
                binding = Binding(job_id, run_id, msg.dealer_id, addresses([msg.to])[0], f"[{engine.tag}]",
                    evidence.received_at, message_id, evidence.message_id, evidence.thread_id)
                self.store.db.execute("INSERT INTO engine_mail_bindings VALUES (?,?,?,?,?)", (*key, _json(asdict(binding))))
            stored_result = {"sent": result.sent, "sent_at": result.sent_at, "reason_code": result.reason_code,
                "audit": asdict(result.audit) if result.audit else None}
            self.store.db.execute("UPDATE engine_outbox SET status=?,result=? WHERE account=? AND job_id=? AND run_id=? AND message_id=?",
                ("sent" if result.sent and result.reason_code is None else "review", _json(stored_result), *key))
            self.store._save(self.account, engine)
        return "sent" if result.sent and result.reason_code is None else "review"
