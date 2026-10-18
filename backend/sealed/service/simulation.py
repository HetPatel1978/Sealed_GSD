"""Durable synthetic integrations for dashboard jobs; never constructs live clients."""

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from email.message import EmailMessage

from sealed.core.email_policy import format_draft
from sealed.core.engine import SendResult
from sealed.core.models import EmailAudit, MailEvidence, outbound_fingerprint
from sealed.ingest.deliver import MAIL
from sealed.ingest.mail import decode_message
from sealed.parser.quote import Extraction, ParserError, validate
from sealed.sim.email import compose


class Simulation:
    address = "buyer@example.com"
    model = "offline-dashboard-extractor"

    def __init__(self, store, initial_time):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS api_sim_clock (id INTEGER PRIMARY KEY CHECK(id=1), now TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_sim_mail (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        """)
        store.db.execute("INSERT OR IGNORE INTO api_sim_clock VALUES (1,?)", (initial_time.isoformat(),))

    def clock(self):
        return datetime.fromisoformat(self.store.db.execute("SELECT now FROM api_sim_clock WHERE id=1").fetchone()[0])

    def advance(self, minutes):
        self.store.db.execute("UPDATE api_sim_clock SET now=? WHERE id=1", ((self.clock() + timedelta(minutes=minutes)).isoformat(),))

    def search(self, query):
        sender = re.search(r"from:([^ ]+)", query)
        return [str(row[0]) for row in self.store.db.execute("SELECT id,payload FROM api_sim_mail ORDER BY id")
                if sender and MAIL.validate_json(row[1]).from_addr.casefold() == sender.group(1).casefold()]

    def fetch(self, uid):
        return MAIL.validate_json(self.store.db.execute("SELECT payload FROM api_sim_mail WHERE id=?", (uid,)).fetchone()[0])

    def parse(self, text, context):
        match = re.search(r"Out-the-door total: ([\d.]+) (USD|CAD|EUR|GBP) including all fees and taxes\.", text)
        available = re.search(r"Availability: (\d{4}-\d{2}-\d{2})\.", text)
        expiry = re.search(r"Valid through (\d{4}-\d{2}-\d{2})\.", text)
        if not match or not available or not expiry:
            raise ParserError("invalid_simulated_reply")
        extraction = Extraction.model_validate({
            "price": {"amount": float(match.group(1)), "evidence": match.group(0)},
            "currency": match.group(2), "out_the_door": True, "out_the_door_evidence": match.group(0),
            "spec_status": "matches", "spec_evidence": context.specification,
            "available": {"date": available.group(1), "evidence": available.group(0)},
            "valid_until": {"date": expiry.group(1), "evidence": expiry.group(0)},
            "hedge_words": [], "declined": False, "decline_evidence": "", "suspicious": False,
            "confirms_booking": "Booking is confirmed." in text,
            "booking_evidence": "Booking is confirmed." if "Booking is confirmed." in text else "", "breakdown": None})
        return validate(extraction, text, context)

    def transport(self, engine):
        return SimulatedTransport(self, engine)


class SimulatedTransport:
    def __init__(self, simulation, engine):
        if engine.job.mode != "sim":
            raise ValueError("simulation_requires_sim_job")
        self.simulation, self.engine = simulation, engine

    def send(self, now, msg, approve_preview):
        sim, eng = self.simulation, self.engine
        if msg.channel != "email":
            return SendResult(False, now, reason_code="fo_failed")
        draft = compose(msg)
        if not approve_preview(format_draft(draft)):
            return SendResult(False, now, reason_code="audit_mismatch")
        thread = str(int.from_bytes(f"{msg.job_id}:{msg.dealer_id}".encode(), "big"))
        sent_id = f"<{msg.job_id}-{msg.message_id}@fo.example>"
        evidence = MailEvidence(str(900000 + len(eng.log.events)), thread, sent_id, sim.clock().isoformat(),
            "pass", "pass", "pass", "synthetic_dashboard")
        audit = EmailAudit(msg.job_id, msg.run_id, msg.message_id, outbound_fingerprint(msg),
            "passed", None, "brief_constraints", True, True, (), (), evidence)
        if msg.kind in {"rfq", "firm_up", "counter", "price_match", "booking"}:
            rank = next(i for i, d in enumerate(eng.job.dealers) if d.id == msg.dealer_id)
            price = round(eng.job.target * (1.10 + rank * .02), 2) if msg.kind == "rfq" else round(eng.job.target, 2)
            if msg.kind == "booking":
                price = eng.proposal["price_total"]
            text = (("Booking is confirmed. " if msg.kind == "booking" else "") +
                f"Quote for {eng.job.details}. Out-the-door total: {price:.2f} {eng.job.currency} including all fees and taxes. "
                f"Availability: {eng.job.deadline}. Valid through {eng.job.deadline}.")
            wire = EmailMessage()
            wire["From"], wire["To"], wire["Cc"] = msg.to, "fo@wajo.ai", sim.address
            wire["Subject"] = msg.subject if msg.subject.lower().startswith("re:") else "Re: " + msg.subject
            wire["In-Reply-To"], wire["References"] = sent_id, sent_id
            domain = msg.to.rsplit("@", 1)[1]
            wire["Authentication-Results"] = f"mx.google.com; spf=pass; dkim=pass; dmarc=pass header.from={domain}"
            # Commit the synthetic reply once, as part of the transport outcome.
            cursor = sim.store.db.execute("INSERT INTO api_sim_mail(payload) VALUES ('')")
            uid = str(cursor.lastrowid)
            wire["Message-ID"] = f"<sim-{uid}@{domain}>"
            wire.set_content(text)
            mail = decode_message(wire.as_bytes(), gmail_id=uid, uid=uid, thread_id=thread,
                                  received_at=sim.clock().isoformat())
            sim.store.db.execute("UPDATE api_sim_mail SET payload=? WHERE id=?", (json.dumps(asdict(mail)), uid))
        return SendResult(True, now, draft=draft, audit=audit)
