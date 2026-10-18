"""Offline coordinator acceptance: synthetic mailbox, extractor and transport.

Every generated reply is authenticated synthetic evidence for example.org. The
real grounding, guard, ledger, qualifier, checkpoint and outbox code still runs.
No Gmail/Fo/Claude client is constructed. Approval is an explicit simulated user.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory

from sealed.core.email_policy import format_draft
from sealed.core.engine import SendResult, Waits
from sealed.core.models import Dealer, EmailAudit, Job, MailEvidence, outbound_fingerprint
from sealed.ingest.deliver import DeliveryStore
from sealed.ingest.mail import decode_message
from sealed.ingest.store import InboxStore
from sealed.parser.quote import Extraction, validate
from sealed.parser.store import ParseStore
from sealed.service.coordinator import Coordinator, Policy
from sealed.sim.email import compose

OWNER = "buyer@example.com"
SELLER = "seller@example.org"
SPEC = "sample quote for the integration test"


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, minutes=1):
        self.now += timedelta(minutes=minutes)


class Mailbox:
    address = OWNER

    def __init__(self):
        self.mail = {}

    def search(self, query):
        sender = re.search(r"from:([^ ]+)", query)
        return [uid for uid, message in self.mail.items() if not sender or message.from_addr == sender.group(1)]

    def fetch(self, uid):
        return self.mail[uid]

    def reply(self, outbound, *, price, booking, sent_id, clock):
        number = 100000 + len(self.mail)
        text = (("Booking is confirmed. " if booking else "") + f"Quote for {SPEC}. "
            f"Out-the-door total: ${price:.2f} USD including all fees and taxes. "
            "Availability is October 25, 2026. Valid through October 31, 2026.")
        wire = EmailMessage()
        wire["From"], wire["To"], wire["Cc"] = SELLER, "fo@wajo.ai", OWNER
        wire["Subject"] = outbound.subject if outbound.subject.lower().startswith("re:") else "Re: " + outbound.subject
        wire["Message-ID"] = f"<reply-{number}@example.org>"
        wire["In-Reply-To"] = sent_id
        wire["References"] = sent_id
        wire["Authentication-Results"] = "mx.google.com; spf=pass; dkim=pass; dmarc=pass header.from=example.org"
        wire.set_content(text)
        self.mail[str(number)] = decode_message(wire.as_bytes(), gmail_id=str(number), uid=str(number), thread_id="555",
            received_at=(clock() + timedelta(seconds=1)).isoformat())


class Extractor:
    model = "offline-demo-extractor"

    def __init__(self):
        self.calls = 0

    def parse(self, text, context):
        self.calls += 1
        match = re.search(r"Out-the-door total: \$(\d+\.\d+) USD including all fees and taxes\.", text)
        raw = Extraction.model_validate({"price": {"amount": float(match.group(1)), "evidence": match.group(0)},
            "currency": "USD", "out_the_door": True, "out_the_door_evidence": match.group(0),
            "spec_status": "matches", "spec_evidence": SPEC,
            "available": {"date": "2026-10-25", "evidence": "Availability is October 25, 2026."},
            "valid_until": {"date": "2026-10-31", "evidence": "Valid through October 31, 2026."},
            "hedge_words": [], "declined": False, "decline_evidence": "", "suspicious": False,
            "confirms_booking": "Booking is confirmed." in text,
            "booking_evidence": "Booking is confirmed." if "Booking is confirmed." in text else "", "breakdown": None})
        return validate(raw, text, context)


class Transport:
    def __init__(self, mailbox, clock):
        self.mailbox, self.clock = mailbox, clock
        self.sent = []

    def send(self, now, msg, approve_preview):
        draft = compose(msg)
        if not approve_preview(format_draft(draft)):
            return SendResult(False, now, reason_code="audit_mismatch")
        self.sent.append(msg)
        sent_id = f"<outbound-{msg.message_id}@fo.example>"
        evidence = MailEvidence(str(200000 + len(self.sent)), "555", sent_id, self.clock().isoformat(),
            "pass", "pass", "pass", "synthetic_demo")
        audit = EmailAudit(msg.job_id, msg.run_id, msg.message_id, outbound_fingerprint(msg),
            "passed", None, "brief_constraints", True, True, (), (), evidence)
        if msg.kind in {"rfq", "counter", "booking"}:
            self.mailbox.reply(msg, price=100.0 if msg.kind == "rfq" else 90.0,
                booking=msg.kind == "booking", sent_id=sent_id, clock=self.clock)
        return SendResult(True, now, draft=draft, audit=audit)


def run_demo(*, runtime):
    clock, mailbox, extractor = Clock(), Mailbox(), Extractor()
    transport = Transport(mailbox, clock)
    job = Job("coordinator-demo", "controlled demo", SPEC, "19104", "Buyer", "this week", "2026-10-25", 90,
              (Dealer("seller", "Synthetic seller", SELLER),), run_id="demo_01", mode="sim")
    approval_seen = False
    with TemporaryDirectory(prefix="sealed-coordinator-") as directory:
        root = Path(directory)
        def build(store, inbox, cache):
            return Coordinator(store, inbox, cache, account=OWNER, reader=mailbox, extractor=extractor,
                runtime=runtime, transport_factory=lambda eng: transport, policy=Policy(True, True), clock=clock)
        with DeliveryStore(root / "engine.sqlite3") as store, InboxStore(root / "inbox.sqlite3") as inbox, ParseStore(root / "parses.sqlite3") as cache:
            co = build(store, inbox, cache)
            co.register(job, cc=OWNER, tag="SEALED-COORDINATOR-DEMO",
                        waits=Waits(nudge_after=1, round1_close=2, round2_call_after=1, round2_close=4))
            for _ in range(20):
                co.tick()
                eng = store.load(OWNER, job.id, job.run_id, checker=runtime(job.id, job.run_id).checker,
                                 qualifier=runtime(job.id, job.run_id).qualifier)
                if eng.state == "AWAITING_APPROVAL":
                    assert not any(msg.kind == "booking" for msg in transport.sent)
                    approval_seen = True
                    co.approve(job.id, job.run_id, eng.proposal.copy())
                if eng.state == "COMPLETED":
                    break
                clock.advance()
            assert eng.state == "COMPLETED" and approval_seen
            before = (len(transport.sent), extractor.calls, len(eng.log.events))
            final_price = eng.proposal["price_total"]
            timers = store.db.execute("SELECT COUNT(*) FROM coordinator_timers").fetchone()[0]
        # Reopen all durable state and poll duplicates without repeating actions.
        with DeliveryStore(root / "engine.sqlite3") as store, InboxStore(root / "inbox.sqlite3") as inbox, ParseStore(root / "parses.sqlite3") as cache:
            build(store, inbox, cache).tick()
            restored = store.load(OWNER, job.id, job.run_id, checker=runtime(job.id, job.run_id).checker,
                                  qualifier=runtime(job.id, job.run_id).qualifier)
            after = (len(transport.sent), extractor.calls, len(restored.log.events))
            assert before == after
        return {"mode": "sim", "state": restored.state, "final_price": final_price,
            "outreach": [msg.kind for msg in transport.sent], "parser_calls": extractor.calls,
            "timers_fired": timers, "approval_source": "explicit_simulated_user", "restart_replay_safe": before == after,
            "events": after[2], "live_network_requests": 0, "live_messages_sent": 0}
