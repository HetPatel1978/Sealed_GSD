"""Serialized application service. Only public job data enters this module."""

import json
import os
from contextlib import ExitStack
from datetime import datetime, timezone

from sealed.core.ledger import latest_written
from sealed.core.models import Dealer, Job
from sealed.fo.messages import new_tag
from sealed.fo.adapter import Store as FoStore, _TURN_LOCK
from sealed.discovery.models import DiscoveryError
from sealed.ingest.deliver import DeliveryStore
from sealed.ingest.gmail import GmailError, GmailReader
from sealed.ingest.store import InboxStore
from sealed.parser.quote import DEFAULT_MODEL, QuoteParser
from sealed.parser.store import ParseStore
from sealed.service.coordinator import Coordinator, Policy
from sealed.service.live import LiveTransport
from sealed.service.lock import worker_lock
from sealed.service.simulation import Simulation


class APIError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class JobService:
    """Owned and called exclusively by the API's single executor thread."""

    def __init__(self, root, *, mode, runtime, fo_state):
        self.root, self.mode, self.runtime, self.fo_state = root, mode, runtime, fo_state
        self.stack = ExitStack()
        self.owned_jobs = set()
        self.discovery = None
        try:
            self.stack.enter_context(worker_lock(root / "api.worker.lock"))
            if mode == "live":
                self.stack.enter_context(worker_lock(fo_state.with_suffix(fo_state.suffix + ".worker.lock")))
            self.store = self.stack.enter_context(DeliveryStore(root / "engine.sqlite3"))
            self.inbox = self.stack.enter_context(InboxStore(root / "inbox.sqlite3"))
            self.cache = self.stack.enter_context(ParseStore(root / "parses.sqlite3"))
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS api_jobs (
                    job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, account TEXT NOT NULL,
                    started INTEGER NOT NULL DEFAULT 0, policy TEXT NOT NULL DEFAULT '{}',
                    worker_status TEXT NOT NULL DEFAULT 'created');
            """)
            self.simulation = Simulation(self.store, datetime.now(timezone.utc)) if mode == "sim" else None
            self.account = self.simulation.address if self.simulation else os.getenv("GMAIL_ADDRESS", "").casefold()
            self.model = os.getenv("ANTHROPIC_QUOTE_MODEL") or DEFAULT_MODEL
            self.extractor = None  # construct a paid integration only for an authorized started run
            self.integrations = {name: "not_checked" if mode == "live" else "not_configured" for name in ("fo", "gmail")}
        except BaseException:
            self.stack.close()
            raise

    def close(self):
        self.stack.close()

    def clock(self):
        return self.simulation.clock() if self.simulation else datetime.now(timezone.utc)

    def row(self, job_id):
        row = self.store.db.execute("SELECT run_id,account,started,policy,worker_status FROM api_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise APIError("job_not_found", 404)
        if row[1] != self.account:
            raise APIError("job_account_unavailable", 409)
        return row

    def engine(self, job_id):
        run_id = self.row(job_id)[0]
        runtime = self.runtime(job_id, run_id)
        return self.store.load(self.account, job_id, run_id, checker=runtime.checker, qualifier=runtime.qualifier)

    def coordinator(self, reader, policy):
        if self.simulation:
            extractor, transport = self.simulation, self.simulation.transport
        else:
            if policy.allow_model_requests and self.extractor is None:
                self.extractor = self.stack.enter_context(QuoteParser(os.getenv("ANTHROPIC_API_KEY", ""), self.model))
            extractor = self.extractor if policy.allow_model_requests else None
            transport = lambda engine: LiveTransport(reader, engine, state=self.fo_state, audits=self.root / "audits.sqlite3")
        return Coordinator(self.store, self.inbox, self.cache, account=self.account, reader=reader,
            extractor=extractor, runtime=self.runtime, transport_factory=transport, policy=policy,
            model=self.model, clock=self.clock)

    def local_coordinator(self, job_id):
        row = self.row(job_id)
        class AccountOnly:
            address = self.account
        return self.coordinator(self.simulation or AccountOnly(), Policy())

    def create(self, job_id, data):
        vendors = data["vendors"]
        if any(v["source"]["kind"] == "discovery" for v in vendors):
            if self.discovery is None:
                raise APIError("discovery_selection_changed", 422)
            try:
                self.discovery.validate_selection(data)
            except DiscoveryError:
                raise APIError("discovery_selection_changed", 422) from None
        if any(v["mode"] != self.mode for v in vendors):
            raise APIError("vendor_mode_mismatch", 422)
        if self.mode == "sim" and any(not v["email"] for v in vendors):
            raise APIError("simulation_requires_email", 422)
        if not self.account:
            raise APIError("gmail_not_configured", 503)
        dealers = tuple(Dealer(f"v_{i+1:02d}", v["name"], v["email"], v["phone"], self.mode,
                               v["source"]["kind"], tuple(v["source"]["urls"])) for i, v in enumerate(vendors))
        job = Job(job_id, data["item"], data["spec"]["details"], data["zip"], data["buyer"], data["timeline"],
                  data["deadline"], data["target"], dealers, data["currency"], data["max_rounds"], mode=self.mode)
        class AccountOnly:
            address = self.account
        co = self.coordinator(self.simulation or AccountOnly(), Policy())
        co.register(job, cc=self.account, tag=new_tag())
        self.store.db.execute("INSERT INTO api_jobs(job_id,run_id,account) VALUES (?,?,?)", (job.id, job.run_id, self.account))
        self.owned_jobs.add(job.id)
        return self.view(job.id)

    def list_jobs(self):
        return [self.view(row[0]) for row in self.store.db.execute("SELECT job_id FROM api_jobs WHERE account=? ORDER BY rowid DESC", (self.account,))]

    def integration_status(self):
        return self.integrations.copy()

    def view(self, job_id):
        eng = self.engine(job_id)
        runtime = self.runtime(job_id, eng.job.run_id)
        current = {q.dealer_id: q for q in latest_written(eng.ledger.quotes)}
        vendors = []
        active_calls = {m.dealer_id for m in self.store.pending_calls(self.account, eng.job.id, eng.job.run_id)}
        for d in eng.job.dealers:
            quote = current.get(d.id)
            sent = [e for e in eng.log.of_type("message.sent") if e["vendor_id"] == d.id]
            state = "CONTACTED" if d.id in eng.contacted else "CREATED"
            if quote:
                state = "QUOTE_RECEIVED"
            if sent and sent[-1]["data"]["kind"] in {"counter", "price_match"}:
                state = "COUNTERED"
            if d.id in active_calls:
                state = "CALL_IN_PROGRESS"
            if eng.proposal and eng.proposal["vendor_id"] == d.id:
                state = {"AWAITING_APPROVAL": "SELECTED", "BOOKING": "BOOKING_SENT", "COMPLETED": "VERIFIED"}.get(eng.state, state)
            if d.id in eng.flagged:
                state = "NEEDS_HUMAN"
            if eng.state == "CANCELLED":
                state = "CANCELLED"
            flags = (["suspicious_instruction"] if d.id in eng.flagged else [])
            if quote:
                flags += list(quote.hedge_words) + (["declined"] if quote.declined else [])
            vendors.append({"id": d.id, "name": d.name, "email": eng.email_to(d.id), "phone": d.phone,
                "mode": d.mode, "state": state, "flags": flags,
                "latest_quote": None if quote is None else {"quote_id": quote.quote_id, "price_total": quote.price_total,
                    "price_unit": quote.price_total, "derived_total": False, "currency": eng.job.currency, "firm": quote.firm,
                    "delivery_date": quote.available, "meets_deadline": quote.meets_deadline, "meets_spec": quote.meets_spec,
                    "verification": "VERIFIED" if quote.verified else "UNVERIFIED"}})
        started = eng.log.of_type("job.started")
        return {"id": eng.job.id, "run_id": eng.job.run_id, "item": eng.job.item,
            "spec": {"quantity": 1, "details": eng.job.details}, "zip": eng.job.zip, "deadline": eng.job.deadline,
            "currency": eng.job.currency, "target": eng.job.target, "ceiling_set": runtime.ready,
            "max_rounds": eng.job.max_rounds, "round": eng.round, "mode": eng.job.mode, "state": eng.state,
            "started_at": started[0]["ts"] if started else None, "vendors": vendors, "proposal": eng.proposal,
            "first_quote": eng.first_quote(), "best_quote": eng.proposal["price_total"] if eng.proposal else None,
            "trust": eng.trust, "last_event_id": len(eng.log.events), "worker_status": self.row(job_id)[4],
            "review": self.local_coordinator(job_id).reviewable((job_id, eng.job.run_id), eng),
            "start_requested": bool(self.row(job_id)[2])}

    def events(self, job_id, after=0):
        row = self.row(job_id)
        return [json.loads(item[0]) for item in self.store.db.execute(
            "SELECT event FROM engine_events WHERE account=? AND job_id=? AND run_id=? AND event_id>? ORDER BY event_id",
            (self.account, job_id, row[0], after))]

    def start(self, job_id, command):
        row = self.row(job_id)
        eng = self.engine(job_id)
        if row[2] or eng.state != "CREATED":
            raise APIError("job_already_started")
        if not self.runtime(job_id, eng.job.run_id).ready:
            raise APIError("private_runtime_required")
        if self.mode == "live" and not (command["allow_dispatch"] and command["allow_model_requests"]):
            raise APIError("live_permissions_required")
        if self.mode == "live" and any(not d.email for d in eng.job.dealers) and not command["allow_calls"]:
            raise APIError("call_authorization_required")
        policy = Policy(True, True) if self.mode == "sim" else Policy(command["allow_model_requests"], command["allow_dispatch"], command["allow_calls"])
        self.store.db.execute("UPDATE api_jobs SET started=1,policy=?,worker_status='scheduled' WHERE job_id=?",
                              (json.dumps(policy.__dict__), job_id))
        self.tick(job_id)
        return self.view(job_id)

    def tick(self, job_id=None):
        if self.mode == "live":
            if not _TURN_LOCK.acquire(blocking=False):
                self.store.db.execute("UPDATE api_jobs SET worker_status='fo_busy_research' WHERE account=? AND started=1", (self.account,))
                return
            try:
                return self._tick(job_id)
            finally:
                _TURN_LOCK.release()
        return self._tick(job_id)

    def _tick(self, job_id=None):
        ids = [job_id] if job_id else [r[0] for r in self.store.db.execute("SELECT job_id FROM api_jobs WHERE account=? AND started=1", (self.account,))]
        for id_ in ids:
            row = self.row(id_)
            if not row[2]:
                continue
            eng = self.engine(id_)
            if self.mode == "live" and self.store.pending_calls(self.account, id_, row[0]):
                # Read-only call observation does not need IMAP, the parser or a
                # restored ceiling. The stored call already had dialing permission.
                status = self.local_coordinator(id_).poll_calls((id_, row[0]))
                self.store.db.execute("UPDATE api_jobs SET worker_status=? WHERE job_id=?", (status, id_))
                continue
            if eng.state in {"COMPLETED", "CANCELLED", "NEEDS_HUMAN"}:
                self.store.db.execute("UPDATE api_jobs SET worker_status=? WHERE job_id=?", (eng.state.lower(), id_))
                continue
            if self.mode == "live":
                if FoStore(self.fo_state).data.get("active") and not self.store.pending_calls(self.account):
                    self.store.db.execute("UPDATE api_jobs SET worker_status='fo_account_requires_review' WHERE job_id=?", (id_,))
                    continue
            if not self.runtime(id_, row[0]).ready:
                self.store.db.execute("UPDATE api_jobs SET worker_status='private_runtime_required' WHERE job_id=?", (id_,))
                continue
            policy = Policy(**json.loads(row[3]))
            try:
                if self.simulation:
                    report = self.coordinator(self.simulation, policy).tick(scopes=[(id_, row[0])])
                else:
                    with GmailReader(self.account, os.getenv("GMAIL_APP_PASSWORD", "")) as reader:
                        report = self.coordinator(reader, policy).tick(scopes=[(id_, row[0])])
                    self.integrations["gmail"] = "down" if report["status"] == "mail_poll_failed" else "live"
                    if report["dispatched"]:
                        self.integrations["fo"] = "live"
                status = report["status"]
                if status == "ok" and report["runs"]:
                    status = report["runs"][0]["status"]
            except (GmailError, OSError, TimeoutError):
                status = "mail_poll_failed"
                if not self.simulation:
                    self.integrations["gmail"] = "down"
            self.store.db.execute("UPDATE api_jobs SET worker_status=? WHERE job_id=?", (status, id_))

    def approve(self, job_id, echo):
        eng = self.engine(job_id)
        if not self.runtime(job_id, eng.job.run_id).ready:
            raise APIError("private_runtime_required")
        # Refresh live mail before accepting terms, including while awaiting approval.
        if self.mode == "live":
            self.tick(job_id)
            if self.row(job_id)[4] not in {"awaiting_approval", "running"}:
                raise APIError("pending_integration_review")
        eng = self.engine(job_id)
        self.local_coordinator(job_id).approve(job_id, eng.job.run_id, echo)
        return self.view(job_id)

    def reject(self, job_id, proposal_id, reason):
        eng = self.engine(job_id)
        if not self.runtime(job_id, eng.job.run_id).ready:
            raise APIError("private_runtime_required")
        # User-supplied free text is not a public event or an outbound message.
        self.local_coordinator(job_id).reject(job_id, eng.job.run_id, proposal_id, "Rejected by the buyer.")
        return self.view(job_id)

    def cancel(self, job_id):
        eng = self.engine(job_id)
        self.local_coordinator(job_id).cancel(job_id, eng.job.run_id)
        return self.view(job_id)

    def resolve(self, job_id, command):
        """Settle one unclear Fo send/call from the dashboard once the buyer has checked Fo and Gmail."""
        row = self.row(job_id)
        message_id, outcome = command["message_id"], command["outcome"]
        co = self.local_coordinator(job_id)
        review = co.reviewable((job_id, row[0]))
        if not review or review["message_id"] != message_id or (outcome == "skip" and not review["can_skip"]):
            raise APIError("review_not_available")
        if self.mode == "live":
            with _TURN_LOCK:
                fo = FoStore(self.fo_state)
                key = FoStore.key(job_id, row[0], message_id)
                active = fo.data.get("active")
                if active not in (None, key):
                    raise APIError("fo_account_requires_review")
                # Keep the original status, reply and fingerprint; the retry gets its own message ID.
                record = fo.data["messages"].setdefault(key, {})
                record.setdefault("resolutions", []).append({"kind": "buyer_checked_in_dashboard", "outcome": outcome,
                    "reviewed_at": datetime.now(timezone.utc).isoformat()})
                fo.data["active"] = None
                fo.save()
        co.resolve(job_id, row[0], message_id, outcome)
        self.store.db.execute("UPDATE api_jobs SET worker_status='scheduled' WHERE job_id=?", (job_id,))
        return self.view(job_id)

    def allow_calls(self, job_id):
        row = self.row(job_id)
        if self.mode != "live" or not row[2] or self.engine(job_id).state != "RUNNING":
            raise APIError("job_not_available")
        policy = Policy(**json.loads(row[3]))
        if not policy.allow_dispatch or not policy.allow_model_requests:
            raise APIError("live_permissions_required")
        updated = Policy(True, True, True)
        self.store.db.execute("UPDATE api_jobs SET policy=?,worker_status='scheduled' WHERE job_id=?",
                              (json.dumps(updated.__dict__), job_id))
        return self.view(job_id)

    def advance(self, job_id, steps):
        if not self.simulation:
            raise APIError("not_found", 404)
        eng = self.engine(job_id)
        if not self.row(job_id)[2]:
            raise APIError("job_not_started")
        released = 0
        for _ in range(100 if steps == -1 else steps):
            eng = self.engine(job_id)
            if eng.state in {"AWAITING_APPROVAL", "NEEDS_HUMAN", "COMPLETED", "CANCELLED"}:
                break
            before = len(eng.log.events)
            self.tick(job_id)
            eng = self.engine(job_id)
            if len(eng.log.events) == before:
                unfired = [(deadline, name) for deadline, name in eng.timers() if not self.store.db.execute(
                    "SELECT 1 FROM coordinator_timers WHERE account=? AND job_id=? AND run_id=? AND name=?",
                    (self.account, job_id, eng.job.run_id, name)).fetchone()]
                if unfired and eng.state == "RUNNING":
                    minutes = (self.clock() - eng.log.t0).total_seconds() / 60
                    self.simulation.advance(max(0, min(t for t, _ in unfired) - minutes))
                    self.tick(job_id)
            released += 1
        eng = self.engine(job_id)
        return {"released": released, "pending": int(eng.state in {"RUNNING", "BOOKING"}), "state": eng.state}

    def receipt(self, job_id):
        eng = self.engine(job_id)
        if eng.state != "COMPLETED":
            raise APIError("receipt_not_ready")
        data = eng.log.of_type("job.completed")[-1]["data"]
        confirmations = [e for e in eng.log.of_type("quote.verified") if e["data"]["subject_id"] == eng.booking_message]
        return {"job_id": job_id, "vendor_id": eng.winner, "vendor_name": eng.job.dealer(eng.winner).name,
            "price_total": eng.proposal["price_total"], "currency": eng.job.currency,
            "delivery_date": eng.proposal["delivery_date"], **data, "verification": "VERIFIED",
            "evidence": [{**e["data"]["evidence"], "label": "Seller written confirmation"} for e in confirmations], "mode": eng.job.mode}
