"""Run one account's fixed-round negotiations with injected integrations.

Tick order is poll -> extract/deliver -> dispatch -> one due timer. An inbox or
transport failure cannot advance the negotiation using stale terms. External
calls never run inside a checkpoint transaction. No private ceiling is accepted.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event

from sealed.core.engine import RESOLVABLE, Engine, StaleProposal, Waits
from sealed.core.events import EventLog
from sealed.ingest.deliver import DeliveryError, MAIL, QueueTransport, ReplyDelivery, parsing_context
from sealed.ingest.gmail import GmailError, poll
from sealed.ingest.mail import match_message
from sealed.parser.quote import DEFAULT_MODEL, ParserError, input_digest


@dataclass(frozen=True)
class Policy:
    allow_model_requests: bool = False
    allow_dispatch: bool = False
    allow_calls: bool = False


@dataclass(frozen=True)
class Runtime:
    checker: object
    qualifier: object
    ready: bool = True


class Coordinator:
    def __init__(self, store, inbox, cache, *, account, reader, extractor, runtime,
                 transport_factory, bindings=(), policy=Policy(), model=DEFAULT_MODEL, clock=None):
        self.store, self.inbox, self.cache = store, inbox, cache
        self.account, self.reader, self.extractor = account.casefold(), reader, extractor
        self.runtime, self.transport_factory = runtime, transport_factory
        self.bindings, self.policy = list(bindings), policy
        self.model = extractor.model if extractor is not None else model
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS coordinator_timers (
                account TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                name TEXT NOT NULL, fired_at TEXT NOT NULL,
                PRIMARY KEY(account,job_id,run_id,name));
            CREATE TABLE IF NOT EXISTS coordinator_controls (
                account TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                cancelled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account,job_id,run_id));
        """)

    def scopes(self):
        return list(self.store.db.execute("SELECT job_id,run_id FROM engine_runs WHERE account=? ORDER BY rowid", (self.account,)))

    def _runtime(self, scope):
        return self.runtime(*scope)

    def _load(self, scope, runtime):
        return self.store.load(self.account, *scope, checker=runtime.checker, qualifier=runtime.qualifier)

    def register(self, job, *, cc, tag, waits=Waits()):
        now = self.clock()
        if now.tzinfo is None or cc.casefold() != self.account:
            raise DeliveryError("invalid_coordinator_scope")
        runtime = self._runtime((job.id, job.run_id))
        eng = Engine(job, QueueTransport(), runtime.checker, runtime.qualifier, cc=cc, tag=tag, waits=waits,
            log=EventLog(job.id, job.run_id, job.mode, now))
        eng.create(0)
        self.store.register(self.account, eng)

    def _cancelled(self, scope):
        row = self.store.db.execute("SELECT cancelled FROM coordinator_controls WHERE account=? AND job_id=? AND run_id=?",
                                   (self.account, *scope)).fetchone()
        return bool(row and row[0])

    def cancel(self, job_id, run_id):
        scope = (job_id, run_id)
        runtime = self._runtime(scope)
        with self.store.transaction():
            self._load(scope, runtime)  # reject unknown scope before creating a control
            self.store.db.execute("INSERT INTO coordinator_controls VALUES (?,?,?,1) ON CONFLICT(account,job_id,run_id) DO UPDATE SET cancelled=1",
                                  (self.account, *scope))
            self._apply_cancel(scope, runtime)

    def _apply_cancel(self, scope, runtime):
        if self.store.in_flight(self.account, *scope):
            return  # the external request may already be running; never overwrite its claim
        eng = self._load(scope, runtime)
        if eng.state in {"COMPLETED", "CANCELLED"}:
            return
        for msg in self.store.outbox(self.account, *scope):
            self.store.db.execute("UPDATE engine_outbox SET status='cancelled' WHERE account=? AND job_id=? AND run_id=? AND message_id=?",
                                  (self.account, *scope, msg.message_id))
            eng.queued_messages.discard(msg.message_id)
        eng.state, eng.proposal = "CANCELLED", None
        eng.log.emit(self._minutes(eng), "job.cancelled", None, {"reason": "User cancelled the job."})
        self.store._save(self.account, eng)

    def reviewable(self, scope, engine=None):
        """The one unclear Fo send or call behind this run's latest pause, or None.

        Only a pause that a buyer can settle by checking Fo and Gmail qualifies; guard,
        identity, parser and budget pauses keep their own review paths.
        """
        eng = engine or self._load(scope, self._runtime(scope))
        if eng.state != "NEEDS_HUMAN" or self._cancelled(scope) or self.store.in_flight(self.account, *scope):
            return None
        rows = self.store.db.execute("SELECT message_id,result FROM engine_outbox WHERE account=? AND job_id=? AND run_id=? AND status='review'",
                                     (self.account, *scope)).fetchall()
        if len(rows) != 1:
            return None
        message_id, result = rows[0][0], json.loads(rows[0][1] or "null") or {}
        msg, code = eng.outbound.get(message_id), result.get("reason_code")
        blocking = [e for e in eng.log.of_type("job.needs_human") if e["data"].get("blocking")]
        if msg is None or code not in RESOLVABLE or not blocking:
            return None
        if code == "payment_mention" and result.get("sent") is not False:
            return None  # Fo reported sending text with payment language: keep the closer review path

        last = blocking[-1]
        if (last["vendor_id"] != msg.dealer_id or last["data"].get("reason_code") != code
                or last["data"].get("message_id", message_id) != message_id
                or any(e["id"] > last["id"] for e in eng.log.of_type("job.resumed"))):
            return None
        return {"message_id": message_id, "vendor_id": msg.dealer_id, "channel": msg.channel, "kind": msg.kind,
                "reason_code": code, "event_id": last["id"], "can_skip": msg.kind != "booking"}

    def resolve(self, job_id, run_id, message_id, outcome):
        scope = (job_id, run_id)
        runtime = self._runtime(scope)
        with self.store.transaction():
            eng = self._load(scope, runtime)
            review = self.reviewable(scope, eng)
            if not review or review["message_id"] != message_id or (outcome == "skip" and not review["can_skip"]):
                raise DeliveryError("review_not_available")
            eng.resolve_review(self._minutes(eng), message_id, outcome, review["event_id"])
            self.store.db.execute("UPDATE engine_outbox SET status=? WHERE account=? AND job_id=? AND run_id=? AND message_id=?",
                                  (f"resolved_{outcome}", self.account, *scope, message_id))
            self.store._save(self.account, eng)

    def _minutes(self, engine):
        return (self.clock() - engine.log.t0).total_seconds() / 60

    def _records(self, scope, status="ready"):
        records = []
        for item in self.inbox.pending(self.account, status=status):
            binding = item["match"].get("binding")
            if binding and (binding["job_id"], binding["run_id"]) == scope:
                records.append(item)
            elif status == "review" and binding is None:
                records.append(item)
        return sorted(records, key=lambda item: (item["message"]["received_at"], int(item["message"]["gmail_id"])))

    def _pause(self, scope, runtime, code):
        with self.store.transaction():
            eng = self._load(scope, runtime)
            if eng.state in {"COMPLETED", "CANCELLED", "NEEDS_HUMAN"}:
                return
            eng.state, eng.proposal = "NEEDS_HUMAN", None
            eng.log.emit(self._minutes(eng), "job.needs_human", None, {"reason_code": code, "blocking": True,
                "message": "The coordinator paused this job before further outreach. Review the pending integration or reply."})
            self.store._save(self.account, eng)

    def _service(self, scope, runtime):
        return ReplyDelivery(self.store, self.inbox, self.cache, account=self.account,
            bindings=self.bindings, model=self.model, checker=runtime.checker, qualifier=runtime.qualifier)

    def _all_bindings(self, scopes):
        selected = set(scopes)
        bindings = [b for b in self.bindings if b.scope[:2] in selected]
        for scope in scopes:
            bindings.extend(self.store.trusted_bindings(self.account, *scope))
        return bindings

    def _advance(self, scope, runtime):
        with self.store.transaction():
            eng = self._load(scope, runtime)
            if self._cancelled(scope) or self.store.in_flight(self.account, *scope) or self.store.pending_calls(self.account, *scope):
                return None
            if self.store.outbox(self.account, *scope):
                return None
            minutes = self._minutes(eng)
            if eng.state == "CREATED":
                if not self.policy.allow_dispatch:
                    return "start_authorization_required"
                eng.start(minutes)
                self.store._save(self.account, eng)
                return "started"
            if eng.state != "RUNNING":
                return None
            for deadline, name in sorted(eng.timers()):
                key = (self.account, *scope, name)
                if minutes < deadline or self.store.db.execute(
                        "SELECT 1 FROM coordinator_timers WHERE account=? AND job_id=? AND run_id=? AND name=?", key).fetchone():
                    continue
                try:
                    eng.on_timer(minutes, name)
                except Exception:
                    # Discard partial transition; private state may need restoring.
                    eng = self._load(scope, runtime)
                    eng.state, eng.proposal = "NEEDS_HUMAN", None
                    eng.log.emit(minutes, "job.needs_human", None, {"reason_code": "timer_transition_failed", "blocking": True,
                        "message": "The scheduled transition requires review. No external action was taken."})
                self.store.db.execute("INSERT INTO coordinator_timers VALUES (?,?,?,?,?)", (*key, self.clock().isoformat()))
                self.store._save(self.account, eng)
                return name
        return None

    def poll_calls(self, scope):
        """Observe existing call claims without constructing a parser or sending anything."""
        runtime = self._runtime(scope)
        service = self._service(scope, runtime)
        for msg in self.store.pending_calls(self.account, *scope):
            eng = self._load(scope, runtime)
            service.poll_call(msg.message_id, job_id=scope[0], run_id=scope[1], now=self._minutes(eng),
                              transport=self.transport_factory(eng))
        eng = self._load(scope, runtime)
        if eng.state in {"CANCELLED", "NEEDS_HUMAN"}:
            return eng.state.lower()
        return "call_in_progress" if self.store.pending_calls(self.account, *scope) else eng.state.lower()

    def tick(self, *, scopes=None):
        selected = self.scopes() if scopes is None else list(scopes)
        if self.reader.address.casefold() != self.account:
            raise DeliveryError("mailbox_account_mismatch")
        # Observation continues through restart/cancellation and mail outages.
        # No call permission is repeated, even for a recovered dispatching claim.
        for scope in selected:
            if self.store.pending_calls(self.account, *scope):
                self.poll_calls(scope)
        if self.store.pending_calls(self.account):
            return {"status": "call_in_progress" if any(self.store.pending_calls(self.account, *s) for s in selected) else "fo_busy_call",
                    "runs": [], "dispatched": 0}
        bindings = self._all_bindings(selected)
        try:
            counts = poll(self.reader, self.inbox, bindings) if bindings else None
        except (GmailError, OSError, TimeoutError):
            return {"status": "mail_poll_failed", "runs": [], "dispatched": 0}
        if self.store.db.execute("SELECT 1 FROM engine_outbox WHERE account=? AND status='dispatching'", (self.account,)).fetchone():
            return {"status": "outreach_requires_reconciliation", "runs": [], "dispatched": 0}
        reports, dispatched = [], 0
        for scope in selected:
            runtime = self._runtime(scope)
            report = {"job_id": scope[0], "run_id": scope[1], "delivered": 0, "parsed": 0, "dispatched": 0}
            reports.append(report)
            if self._cancelled(scope):
                with self.store.transaction():
                    self._apply_cancel(scope, runtime)
                report["status"] = "cancelled"
                continue
            eng = self._load(scope, runtime)
            if eng.state in {"COMPLETED", "CANCELLED"}:
                report["status"] = eng.state.lower()
                continue
            if not runtime.ready:
                report["status"] = "private_runtime_required"
                continue
            if self.store.in_flight(self.account, *scope):
                report["status"] = "outreach_requires_reconciliation"
                continue
            if eng.state in {"NEEDS_HUMAN", "COMPLETED", "CANCELLED"}:
                report["status"] = eng.state.lower()
                continue
            if self._records(scope, "review"):
                self._pause(scope, runtime, "inbound_identity_review")
                report["status"] = "needs_human"
                continue
            service = self._service(scope, runtime)
            blocked = False
            for record in self._records(scope):
                mail = MAIL.validate_json(json.dumps(record["message"]), strict=True)
                match = match_message(mail, self._all_bindings([scope]), self.account)
                if match.status != "ready" or not match.binding or match.binding.scope[:2] != scope:
                    self._pause(scope, runtime, "inbound_identity_review")
                    blocked = True
                    break
                eng = self._load(scope, runtime)
                context = parsing_context(eng, mail, match, self.clock())
                digest = input_digest(mail.clean_text, context, self.model)
                cached = self.cache.get(self.account, mail.gmail_id, digest)
                if cached is None:
                    if not self.policy.allow_model_requests or self.extractor is None:
                        report["status"] = "model_authorization_required"
                        blocked = True
                        break
                    try:
                        parsed = self.extractor.parse(mail.clean_text, context)
                        self.cache.put(self.account, mail.gmail_id, digest, parsed)
                        report["parsed"] += 1
                    except ParserError as error:
                        self._pause(scope, runtime, error.code)
                        report["status"] = "needs_human"
                        blocked = True
                        break
                result = service.deliver(mail.gmail_id, job_id=scope[0], run_id=scope[1], now=self.clock())
                if result.status != "applied":
                    report["status"] = result.status
                    blocked = True
                    break
                report["delivered"] += int(not result.duplicate)
            if blocked:
                continue
            # Poll succeeded and all staged replies were applied before any send/timer.
            for msg in self.store.outbox(self.account, *scope):
                if self._cancelled(scope):
                    break
                if not self.policy.allow_dispatch or (msg.channel == "call" and not self.policy.allow_calls):
                    report["status"] = "dispatch_authorization_required"
                    blocked = True
                    break
                eng = self._load(scope, runtime)
                if eng.state == "NEEDS_HUMAN":
                    blocked = True
                    report["status"] = "needs_human"
                    break
                outcome = service.dispatch(msg.message_id, job_id=scope[0], run_id=scope[1], now=self._minutes(eng),
                    transport=self.transport_factory(eng), authorized=True)
                dispatched += int(outcome == "sent")
                report["dispatched"] += int(outcome == "sent")
                if outcome == "review":
                    blocked = True
                    report["status"] = "needs_human"
                    break
                if outcome == "call_pending":
                    blocked = True
                    report["status"] = "call_in_progress"
                    break
            if not blocked:
                report["transition"] = self._advance(scope, runtime)
                report["status"] = self._load(scope, runtime).state.lower()
        return {"status": "ok", "runs": reports, "dispatched": dispatched,
                "mail_staged": counts.staged if counts else 0}

    def approve(self, job_id, run_id, echo):
        scope = (job_id, run_id)
        runtime = self._runtime(scope)
        if not runtime.ready:
            raise DeliveryError("private_runtime_required")
        with self.store.transaction():
            if (self._cancelled(scope) or self._records(scope) or self._records(scope, "review")
                    or self.store.in_flight(self.account, *scope)):
                raise StaleProposal("Pending reply or outreach must settle before approval.")
            eng = self._load(scope, runtime)
            eng.approve(self._minutes(eng), echo)
            self.store._save(self.account, eng)

    def reject(self, job_id, run_id, proposal_id, reason=""):
        scope = (job_id, run_id)
        runtime = self._runtime(scope)
        with self.store.transaction():
            if self._cancelled(scope) or self.store.in_flight(self.account, *scope):
                raise DeliveryError("job_not_available")
            eng = self._load(scope, runtime)
            eng.reject(self._minutes(eng), proposal_id, reason)
            self.store._save(self.account, eng)

    def run(self, *, stop: Event, interval=15, max_cycles=None, on_tick=None):
        if not 0.1 <= interval <= 60:
            raise ValueError("Polling interval must be between 0.1 and 60 seconds.")
        cycles = 0
        while not stop.is_set() and (max_cycles is None or cycles < max_cycles):
            report = self.tick()
            if on_tick:
                on_tick(report)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            stop.wait(interval)
        return cycles
