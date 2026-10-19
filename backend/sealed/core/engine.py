"""Job engine: runs the fixed round plan for one job and writes the event stream (docs/api.md v0.2).

The engine never touches the ceiling. It is given:
  - a `qualifier` callable (bound to the job in the private zone), which answers propose / needs_human,
  - a `checker` object for the guard, which answers yes/no,
  - a `transport` (sim now; the live Fo adapter in step 2).
"""

import re
from dataclasses import dataclass, field, replace
from email.utils import parseaddr
from typing import Callable, Protocol

from sealed.core import strategy
from sealed.core.breakdown import summary as breakdown_summary
from sealed.core.call_review import review_call
from sealed.core.events import EventLog
from sealed.core.guard import Guard
from sealed.core.ledger import Ledger, latest_written, written_order
from sealed.core.models import Decision, EmailAudit, EmailDraft, Inbound, Job, Outbound, Quote
from sealed.fo import messages


@dataclass
class SendResult:
    sent: bool
    sent_at: float
    sent_copy: str | None = None          # the CC copy as delivered (email only)
    errors: list[dict] = field(default_factory=list)   # e.g. a Fo turn timeout that was polled, not resent
    reason_code: str | None = None        # why it wasn't sent (fo_refused, audit_mismatch, send_unclear, ...)
    note: str | None = None               # Fo's own words, for the human
    draft: EmailDraft | None = None       # reviewed draft; not proof of the delivered copy
    audit: EmailAudit | None = None       # authenticated Gmail result, never a Fo self-report
    queued: bool = False                 # durable outbox intent; never evidence of a send
    pending_call: bool = False           # permission issued; observe the existing call, never dial again
    call_report: dict | None = None      # static audited outcome; raw voice transcript stays local to Fo state


class Transport(Protocol):
    def send(self, now: float, msg: Outbound, approve_preview: Callable[[str], bool]) -> SendResult: ...


@dataclass(frozen=True)
class Waits:
    """Fixed timings in minutes from job start. Never depend on the ceiling."""
    nudge_after: float = 180
    round1_close: float = 240
    round2_call_after: float = 180         # after round 1 closes
    round2_close: float = 480


NOT_SENT = {
    "audit_mismatch": "Fo's draft failed the authorized content checks. Nothing was marked sent; please review.",
    "fo_refused": "Fo declined to send this message. Nothing was sent; please review what Fo said.",
    "fo_failed": "Fo reported a failure. Nothing was sent; please review.",
    "send_unclear": "Fo's reply didn't clearly say it was sent. Not resent; check Gmail before retrying.",
    "payment_mention": "Fo's reply mentions a payment, purchase or order. Stopped; please review.",
    "call_plan_rejected": "Fo's call plan did not match the buyer, phone or brief. No permission to dial was issued.",
    "call_review": "The call result needs review for identity, follow-ups or transcript evidence. No call was repeated.",
    "call_result_missing": "The call has no verifiable final result yet. It was not repeated; review the existing Fo conversation.",
}


class StaleProposal(Exception):
    pass


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class Engine:
    def __init__(self, job: Job, transport: Transport, checker, qualifier, *, cc: str, tag: str,
                 waits: Waits = Waits(), log: EventLog | None = None):
        self.job, self.transport, self.qualifier = job, transport, qualifier
        self.guard = Guard(checker)
        self.cc, self.tag, self.waits = cc, tag, waits
        self.log = log or EventLog(job.id, job.run_id, job.mode)
        self.ledger = Ledger(job.deadline, {d.id: d.email for d in job.dealers}, job.currency)
        self.state = "CREATED"
        self.round = 1
        self.started_at = 0.0
        self.contacted: set[str] = set()
        self.nudged: set[str] = set()
        self.called: set[str] = set()
        self.firm_up_sent: set[str] = set()
        self.flagged: set[str] = set()
        self.price_match_sent: dict[str, float] = {}
        self.last_written_at: dict[str, float] = {}
        self.reply_addr: dict[str, str] = {}      # phone-only dealers: first attributed written reply address
        self.sent_keys: set[tuple] = set()
        self.outbound: dict[str, Outbound] = {}
        self.rejected_quotes: set[str] = set()
        self.proposal: dict | None = None
        self.winner: str | None = None
        self.booking_message: str | None = None
        self._n_msg = self._n_reply = self._n_prop = 0
        self.audit_observations: set[str] = set()
        self.queued_messages: set[str] = set()
        self.reply_context: dict[str, dict] = {}
        self.trust = {"budget_revealed_count": 0, "messages_checked_before": 0, "messages_checked_after": 0,
                      "false_claims": 0, "commitments_without_approval": 0, "suspicious_instructions_flagged": 0}

    # ---- lifecycle -------------------------------------------------------------------------------------
    def create(self, now: float) -> None:
        j = self.job
        self.log.emit(now, "job.created", None, {
            "item": j.item, "spec": {"quantity": 1, "details": j.details}, "zip": j.zip, "deadline": j.deadline,
            "currency": j.currency, "target": j.target, "max_rounds": j.max_rounds,
            "vendors": [{"id": d.id, "name": d.name, "email": d.email, "phone": d.phone, "mode": d.mode,
                         "source": {"kind": d.source, **({"urls": list(d.source_urls)} if d.source_urls else {})}}
                        for d in j.dealers]})

    def start(self, now: float, seed: int | None = None) -> float:
        self.state, self.started_at = "RUNNING", now
        self.log.emit(now, "job.started", None, {"seed": seed, "round": 1})
        t = now
        for d in self.job.dealers:
            if d.id in self.contacted:
                continue  # imported trusted outreach must not receive another initial RFQ
            if d.email:
                t = self._send(t, Decision(d.id, "rfq", "rfq",
                                           reason="Round 1 request for a written out-the-door total."), decided=False)
            else:                            # phone-only: the round-1 request is the call (so no later nudge)
                self.nudged.add(d.id)
                t = self._send(t, Decision(d.id, "rfq", "rfq", channel="call", reason=(
                    "Round 1 request for a written out-the-door total. No email on file, so by phone.")),
                    decided=False)
        return t

    def email_to(self, dealer_id: str) -> str | None:
        """The configured email, or a phone-only dealer's address attributed by trusted ingestion."""
        return self.job.dealer(dealer_id).email or self.reply_addr.get(dealer_id)

    def timers(self) -> list[tuple[float, str]]:
        w, s = self.waits, self.started_at
        return [(s + w.nudge_after, "nudge"), (s + w.round1_close, "round1_close"),
                (s + w.round1_close + w.round2_call_after, "round2_calls"), (s + w.round2_close, "round2_close")]

    def on_timer(self, now: float, name: str) -> float:
        if self.state != "RUNNING":
            return now
        t = now
        if name == "nudge":
            for d in strategy.call_nudges(self.job, self.ledger, self.contacted, self.nudged):
                self.nudged.add(d.dealer_id)
                t = self._send(t, d)
        elif name == "round1_close":
            self.round = 2
            for d in strategy.round2(self.job, self.ledger):
                t = self._send(t, d)
                if d.kind == "price_match":
                    self.price_match_sent[d.dealer_id] = t
        elif name == "round2_calls":
            for d in strategy.round2_calls(self.job, self.ledger, self.price_match_sent, self.last_written_at,
                                           self.called):
                self.called.add(d.dealer_id)
                t = self._send(t, d)
        elif name == "round2_close":
            self._qualify(t, "Round 2 closed.")
        return t

    # ---- inbound ---------------------------------------------------------------------------------------
    def on_inbound(self, now: float, inbound: Inbound, *, received_at: float | None = None) -> float:
        self._n_reply += 1
        rid = f"r_{self._n_reply:02d}"
        dealer = self.job.dealer(inbound.dealer_id)
        evidence_ref = (f"gmail:{inbound.mail_evidence.gmail_id}" if inbound.mail_evidence else f"{self.job.mode}-{rid}")
        previous = next((q for q in latest_written(self.ledger.quotes) if q.dealer_id == dealer.id), None)
        source_time = now if received_at is None else received_at
        incoming_order = (source_time, int(inbound.mail_evidence.gmail_id)
                          if inbound.mail_evidence and inbound.mail_evidence.gmail_id.isdigit() else 0)
        not_stale = previous is None or incoming_order >= written_order(previous)
        if (inbound.source == "email" and inbound.contact_verified and inbound.from_addr
                and not self.email_to(dealer.id)):
            contact = self.outbound.get(inbound.in_reply_to or "")
            address = parseaddr(inbound.from_addr)[1]
            if (contact and contact.dealer_id == dealer.id and contact.channel == "call"
                    and dealer.id in self.contacted and address):
                self.reply_addr[dealer.id] = address
                self.ledger.dealer_emails[dealer.id] = address
        src = {"email": "vendor_email", "call": "call", "fo_relay": "fo_relay"}[inbound.source]
        self.log.emit(now, "reply.received", dealer.id, {
            "reply_id": rid, "in_reply_to": inbound.in_reply_to, "source": src,
            "from": inbound.from_addr if inbound.source == "email" else "fo@wajo.ai",
            "subject": inbound.subject if inbound.subject is not None else (
                f"Re: {messages.subject_for(self.job, self.tag, 'rfq')}" if inbound.source == "email" else None),
            "text": inbound.text,
            "evidence": {"kind": "gmail_message" if inbound.source == "email" else "fo_conversation",
                         "ref": evidence_ref}})

        if (self.state == "BOOKING" and dealer.id == self.winner and self.ledger.verified_sender(inbound)
                and not_stale and inbound.parsed.confirms_booking):
            p = self.proposal
            exact = (p is not None and self.booking_message is not None
                     and inbound.in_reply_to == self.booking_message and self.ledger.is_firm(inbound)
                     and not inbound.parsed.suspicious and inbound.parsed.price_total == p["price_total"]
                     and inbound.parsed.currency == p["currency"]
                     and inbound.parsed.available == p["delivery_date"])
            if not exact:
                self.log.emit(now + 0.2, "job.needs_human", dealer.id, {
                    "reason_code": "booking_mismatch", "blocking": True,
                    "message": "The dealer's confirmation does not match the sent booking and approved terms. "
                               "Nothing was marked complete; please review the reply."})
                self.state = "NEEDS_HUMAN"
                return now
            self.log.emit(now + 0.2, "quote.verified", dealer.id, {
                "subject_id": self.booking_message, "status": "VERIFIED",
                "reason": "Dealer's own confirmation email is in your inbox.",
                         "evidence": {"kind": "gmail_message", "ref": evidence_ref}})
            self._complete(now + 0.3)
            return now

        if inbound.source == "call" and inbound.turns:
            sent = self.outbound.get(inbound.in_reply_to or "")
            flags = review_call(list(inbound.turns), sent.body if sent else "")
            if flags:
                self.log.emit(now + 0.1, "job.needs_human", dealer.id, {
                    "reason_code": "call_review", "blocking": False, "flags": flags,
                    "message": f"The call with {dealer.name} went outside the brief or touched on money. "
                               "Review the transcript before relying on anything from it."})

        q = self.ledger.add(rid, inbound, now if received_at is None else received_at)
        current = q in latest_written(self.ledger.quotes)
        if q.verified and current:
            self.last_written_at[dealer.id] = q.received_at
            self.contacted.add(dealer.id)
            if inbound.mail_evidence:
                self.reply_context[dealer.id] = {"evidence": inbound.mail_evidence, "subject": inbound.subject}
        self.log.emit(now + 0.2, "quote.parsed", dealer.id, {
            "reply_id": rid, "quote_id": q.quote_id, "source": "call" if q.source == "call" else "email",
            "price_total": q.price_total, "price_unit": q.price_total, "derived_total": False,
            "currency": inbound.parsed.currency, "firm": q.firm, "hedge_words": list(q.hedge_words),
            "delivery_date": q.available, "meets_deadline": q.meets_deadline, "meets_spec": q.meets_spec,
            "declined": q.declined, "suspicious_instructions": q.suspicious,
            "numbers_grounded": q.numbers_grounded,
            **({"breakdown": q.breakdown, "breakdown_flags": list(q.breakdown_flags)} if q.breakdown else {})})
        if q.price_total is not None and not q.declined:
            verified = q.verified
            self.log.emit(now + 0.3, "quote.verified", dealer.id, {
                "subject_id": q.quote_id, "status": "VERIFIED" if verified else "UNVERIFIED",
                "reason": "Dealer's own email is in your inbox." if verified else
                ("Spoken on a call. Not firm until it arrives in writing." if q.source == "call"
                 else "Email sender does not match the dealer." if q.source == "email"
                 else "Relayed by Fo; not the dealer's own email."),
                "evidence": {"kind": "gmail_message" if q.source == "email" else "fo_conversation",
                             "ref": evidence_ref}})

        if q.verified and not current:
            return now + 0.4  # keep history/evidence; stale mail cannot trigger actions or replace approval

        t = now + 0.4
        d = strategy.after_reply(q, q.dealer_id in self.firm_up_sent, q.dealer_id in self.flagged)
        if d and d.action == "needs_human":
            self.flagged.add(q.dealer_id)
            self.trust["suspicious_instructions_flagged"] = len(self.flagged)
            self._decided(t, replace(d, round=self.round))
            self.log.emit(t + 0.1, "job.needs_human", q.dealer_id, {
                "reason_code": "suspicious_instruction", "blocking": False,
                "message": f"{dealer.name}'s reply claims something Sealed never said or agreed to. "
                           "Nothing was confirmed or paid. Review it before approving this dealer."})
        elif d and d.action == "firm_up" and self.state == "RUNNING":
            self.firm_up_sent.add(q.dealer_id)
            t = self._send(t, replace(d, round=self.round))

        if (self.state == "AWAITING_APPROVAL" and self.proposal and q.verified
                and q.dealer_id == self.proposal["vendor_id"]):
            self._qualify(t + 0.1, "The proposed dealer updated its written terms.")
        elif self.state == "WAITING_AFTER_REJECT" and q.firm:
            self._qualify(t + 0.1, "New firm written quote after a rejected proposal.")
        return t

    # ---- approval --------------------------------------------------------------------------------------
    def approve(self, now: float, echo: dict) -> float:
        p = self.proposal
        keys = ("proposal_id", "vendor_id", "price_total", "currency", "delivery_date")
        if self.state != "AWAITING_APPROVAL" or p is None or any(echo.get(k) != p[k] for k in keys):
            raise StaleProposal("approval does not match the current proposal")
        current = self.ledger.latest_firm(p["vendor_id"])
        if current is None or current.quote_id != p["quote_id"]:
            raise StaleProposal("the quoted terms have been superseded")
        self.log.emit(now, "job.approved", p["vendor_id"], {k: p[k] for k in keys if k != "vendor_id"})
        self.state, self.winner = "BOOKING", p["vendor_id"]
        t = self._send(now + 0.5, Decision(p["vendor_id"], "propose", "booking", round=self.round,
                                           own_quote_id=p["quote_id"], reason="User approved the exact terms."),
                       decided=False, approved_price=p["price_total"])
        if self.state != "BOOKING" or self.booking_message is None:
            return t
        return self._decline_others(t)

    def _decline_others(self, t: float) -> float:
        for d in self.job.dealers:
            if d.id != self.winner and d.id in self.contacted:
                t = self._send(t, Decision(d.id, "propose", "cancel", round=self.round,
                                           reason="Polite decline after approval."), decided=False)
        return t

    def reject(self, now: float, proposal_id: str, reason: str = "") -> None:
        p = self.proposal
        if self.state != "AWAITING_APPROVAL" or p is None or p["proposal_id"] != proposal_id:
            raise StaleProposal("no such current proposal")
        self.log.emit(now, "job.rejected", p["vendor_id"], {"proposal_id": proposal_id, "reason": reason})
        self.rejected_quotes.add(p["quote_id"])
        self.proposal, self.state = None, "WAITING_AFTER_REJECT"

    # ---- internals -------------------------------------------------------------------------------------
    def _decided(self, t: float, d: Decision) -> None:
        data = {"round": d.round, "action": d.action, "offer": d.offer, "reason": d.reason}
        if d.cited_quote_id:
            data |= {"cited_quote_id": d.cited_quote_id, "cited_price": d.cited_price}
        if d.channel == "call":
            data["channel"] = "call"
        self.log.emit(t, "strategy.decided", d.dealer_id, data)

    def _send(self, t: float, d: Decision, decided: bool = True, approved_price: float | None = None) -> float:
        key = (d.dealer_id, d.round, d.kind, d.channel)
        if key in self.sent_keys:            # dedupe: never send the same step twice
            return t
        if self.state == "NEEDS_HUMAN":      # a blocking problem pauses all sending until a human looks
            return t
        if d.channel == "email" and not self.email_to(d.dealer_id):
            return t                         # phone-only dealer that never wrote (e.g. no decline email to send)
        if decided:
            self._decided(t, d)
            t += 0.1
        self._n_msg += 1
        msg = messages.build(self.job, d, f"m_{self._n_msg:02d}", self.tag, self.cc, approved_price,
                             email_to=self.email_to(d.dealer_id))
        context = self.reply_context.get(d.dealer_id)
        if context and msg.email_brief is not None:
            evidence = context["evidence"]
            subject = context["subject"] or msg.subject
            subject = subject if subject.lower().startswith("re:") else "Re: " + subject
            msg = replace(msg, subject=subject, reply_to_message_id=evidence.message_id,
                reply_thread_id=evidence.thread_id, fo_instruction=messages.wrap_email_brief_for_fo(
                    msg.to, msg.cc, subject, msg.body, evidence.message_id))
        pre = self.guard.check(msg)
        self.trust["messages_checked_before"] += 1
        self._guard_event(t, msg, pre)
        if not pre.passed:
            blocking = d.kind == "booking"
            self.log.emit(t + 0.1, "job.needs_human", d.dealer_id, {
                "reason_code": "guard_blocked", "blocking": blocking,
                "message": f"A {d.kind} {d.channel} to {self.job.dealer(d.dealer_id).name} was blocked by the "
                           "pre-send guard and not sent."})
            if blocking:
                self.state = "NEEDS_HUMAN"
            return t + 0.2
        self.sent_keys.add(key)
        self.outbound[msg.message_id] = msg

        def approve_preview(preview: str) -> bool:
            res = self.guard.check(msg, preview=preview)
            self._guard_event(t + 0.5, msg, res)
            return res.passed

        result = self.transport.send(t, msg, approve_preview)
        if result.pending_call:
            self.queued_messages.add(msg.message_id)
            return self.on_send_result(t, msg.message_id, result)
        if result.queued:
            if result.sent or result.reason_code or result.draft or result.audit:
                raise ValueError("Queued transport returned a send outcome.")
            self.queued_messages.add(msg.message_id)
            self.log.emit(t, "message.queued", msg.dealer_id, {"message_id": msg.message_id,
                "kind": msg.kind, "round": msg.round, "channel": msg.channel})
            return t
        return self._finish_send(t, msg, result)

    def on_send_result(self, t: float, message_id: str, result: SendResult) -> float:
        """Apply one queued result; the durable worker commits this with the outbox receipt."""
        if message_id not in self.queued_messages or result.queued:
            raise ValueError("No queued message awaits this result.")
        msg = self.outbound[message_id]
        if result.pending_call:
            if msg.channel != "call" or result.sent or result.reason_code:
                raise ValueError("Invalid pending call result.")
            if not any(event["data"].get("message_id") == message_id for event in self.log.of_type("call.pending")):
                self.log.emit(t, "call.pending", msg.dealer_id, {"message_id": message_id,
                    "state": "awaiting_result", "message": "The call is awaiting Fo's final result. It will not be dialed again."})
            return t
        self.queued_messages.remove(message_id)
        t = self._finish_send(t, msg, result)
        if msg.kind == "booking" and self.state == "BOOKING" and self.booking_message == message_id:
            return self._decline_others(t)
        return t

    def _finish_send(self, t: float, msg: Outbound, result: SendResult) -> float:
        if (msg.email_brief is not None and result.sent
                and (result.draft is None or not self.guard.check_email(msg, result.draft).passed)):
            result.reason_code = result.reason_code or "audit_mismatch"
        safe_note = self.guard.safe_note(msg, result.note)
        if result.call_report is not None:
            self.log.emit(result.sent_at, "call.completed", msg.dealer_id, {
                "message_id": msg.message_id, **result.call_report, "source": "fo_report",
                "written_quote": False})
        for err in result.errors:
            self.log.emit(t + 0.6, "error", msg.dealer_id, err)
        if not result.sent:
            code = result.reason_code or "audit_mismatch"
            self.log.emit(result.sent_at, "job.needs_human", msg.dealer_id, {
                "reason_code": code, "blocking": True, "message": NOT_SENT.get(code, NOT_SENT["send_unclear"]),
                **({"fo_said": safe_note} if safe_note else {})})
            self.state = "NEEDS_HUMAN"
            return result.sent_at
        if result.reason_code is not None:
            self.log.emit(result.sent_at, "job.needs_human", msg.dealer_id, {
                "reason_code": result.reason_code, "blocking": True,
                "message": "Fo reported sending, but the turn needs review. Not resent; check Gmail or the call transcript.",
                **({"fo_said": safe_note} if safe_note else {})})
            self.state = "NEEDS_HUMAN"
        st = result.sent_at
        event_body = msg.body
        content = {}
        if msg.email_brief is not None:
            reviewed = result.draft is not None and self.guard.check_email(msg, result.draft).passed
            event_body = result.draft.body if reviewed else ""
            content = {"brief": msg.body, "content_source": "fo_preview" if reviewed else "pending_gmail"}
        self.log.emit(st, "message.sent", msg.dealer_id, {
            "message_id": msg.message_id, "kind": msg.kind, "round": msg.round, "channel": msg.channel,
            "to": msg.to if msg.channel == "email" else f"{self.job.dealer(msg.dealer_id).name} sales line",
            "subject": msg.subject, "body": event_body, "offer": msg.offer, "cited_quote_id": msg.cited_quote_id,
            "numbers": messages.number_provenance(replace(msg, body=event_body), self.job), **content})
        reached = result.call_report is None or result.call_report.get("outcome") == "connected"
        if msg.dealer_id not in self.contacted and msg.kind == "rfq" and reached:
            self.contacted.add(msg.dealer_id)
            self.log.emit(st, "vendor.contacted", msg.dealer_id, {"message_id": msg.message_id, "round": msg.round})
        if msg.kind == "booking":
            self.booking_message = msg.message_id
        if result.audit is not None:
            self.on_email_audit(st + 1, result.audit)
        elif msg.channel == "email" and result.sent_copy is not None:
            self._audit(st + 1, msg, result.sent_copy)
        return st + 1

    def on_email_audit(self, t: float, result: EmailAudit) -> None:
        """Apply trusted Gmail audit evidence once, scoped to a successfully sent payload."""
        msg = self.outbound.get(result.message_id)
        sent = any(e["data"]["message_id"] == result.message_id for e in self.log.of_type("message.sent"))
        if msg is None or msg.channel != "email" or not sent or not result.validates(msg):
            raise ValueError("Audit does not belong to a sent email in this job/run.")
        if result.state == "pending" or result.observation_id in self.audit_observations:
            return
        self.audit_observations.add(result.observation_id)
        self.trust["messages_checked_after"] += 1
        rules = {rule["rule"]: rule["passed"] for rule in result.rules}
        self.trust["budget_revealed_count"] += int(not rules.get("secret_checker", True) or not rules.get("limit_language", True))
        self.trust["false_claims"] += int(not rules.get("leverage_grounded", True) or not rules.get("facts_grounded", True))
        self.trust["commitments_without_approval"] += int(not rules.get("commitment_words", True))
        self.log.emit(t, "audit.checked", msg.dealer_id, {
            "message_id": msg.message_id, "cc_copy_found": result.cc_copy_found, "matched": result.matched,
            "guard_passed": result.guard_passed, "basis": result.basis,
            "diff": None if result.matched else "Sent-copy verification requires review.",
            "rules": list(result.rules), "reason_code": result.reason_code,
            "evidence": {"kind": "gmail_message", "ref": f"gmail:{result.evidence.gmail_id}"} if result.evidence else None})
        if not result.matched:
            self.state = "NEEDS_HUMAN"
            self.log.emit(t + 0.1, "job.needs_human", msg.dealer_id, {
                "reason_code": "audit_mismatch", "blocking": True,
                "message": "Gmail sent-copy verification requires review. No message was resent."})

    def _guard_event(self, t: float, msg: Outbound, res) -> None:
        self.log.emit(t, "guard.checked", msg.dealer_id, {
            "message_id": msg.message_id, "kind": msg.kind, "round": msg.round, "passed": res.passed,
            "stage": res.stage, "rules": res.rules})

    def _audit(self, t: float, msg: Outbound, copy: str) -> None:
        res = (self.guard.check_email(msg, EmailDraft(msg.to, msg.cc, msg.subject or "", copy))
               if msg.email_brief is not None else self.guard.check(replace(msg, body=copy)))
        rules = {r["rule"]: r["passed"] for r in res.rules}
        matched = res.passed if msg.email_brief is not None else _squash(copy) == _squash(msg.body)
        self.trust["messages_checked_after"] += 1
        self.trust["budget_revealed_count"] += 0 if rules["secret_checker"] and rules["limit_language"] else 1
        self.trust["false_claims"] += 0 if rules["leverage_grounded"] else 1
        self.trust["commitments_without_approval"] += 0 if rules["commitment_words"] else 1
        self.log.emit(t, "audit.checked", msg.dealer_id, {
            "message_id": msg.message_id, "cc_copy_found": True, "matched": matched,
            "diff": None if matched else "Fo's sent copy failed the authorized content checks.", "guard_passed": res.passed,
            **({"basis": "brief_constraints"} if msg.email_brief is not None else {})})
        if not matched or not res.passed:
            blocking = msg.kind == "booking"
            self.log.emit(t + 0.1, "job.needs_human", msg.dealer_id, {
                "reason_code": "audit_mismatch", "blocking": blocking,
                "message": "The copy Fo sent failed the authorized content checks. Not resent; please review."})
            if blocking:
                self.state = "NEEDS_HUMAN"

    def _qualify(self, t: float, why: str) -> None:
        res = self.qualifier(self.ledger.quotes, set(self.flagged), set(self.rejected_quotes))
        if res.action == "propose":
            q: Quote = res.quote
            self._decided(t, Decision(q.dealer_id, "propose", "none", round=self.round, reason=f"{why} {res.reason}"))
            self._n_prop += 1
            first = self.first_quote()
            p = {"proposal_id": f"p_{self._n_prop:02d}", "vendor_id": q.dealer_id, "quote_id": q.quote_id,
                 "price_total": q.price_total, "currency": self.job.currency, "delivery_date": q.available,
                 "flags": list(res.flags)}
            self.proposal = p
            s_abs = round(first - q.price_total, 2) if first else 0.0
            self.log.emit(t + 0.1, "proposal.created", q.dealer_id, {
                "proposal_id": p["proposal_id"], "quote_id": q.quote_id, "price_total": q.price_total,
                "currency": self.job.currency, "delivery_date": q.available, "first_quote": first,
                "savings_abs": s_abs, "savings_pct": round(100 * s_abs / first, 2) if first else 0.0,
                "flags": list(res.flags)})
            self.log.emit(t + 0.2, "approval.requested", q.dealer_id, {"proposal_id": p["proposal_id"]})
            self.state = "AWAITING_APPROVAL"
        else:
            self.proposal = None
            closest = res.quote
            self.log.emit(t, "job.needs_human", closest.dealer_id if closest else None, {
                "reason_code": res.reason_code, "blocking": True, "message": res.reason,
                **({"closest": {"vendor_id": closest.dealer_id, "price_total": closest.price_total,
                                "delivery_date": closest.available, "flags": list(res.flags)}} if closest else {})})
            self.state = "NEEDS_HUMAN"

    def fee_comparison(self) -> list[dict]:
        """Each dealer's latest firm written quote, itemised as the dealer wrote it, lowest total first."""
        rows = []
        for q in sorted(self.ledger.current_firm(), key=lambda q: q.price_total):
            rows.append({"vendor_id": q.dealer_id, "quote_id": q.quote_id, "out_the_door_total": q.price_total,
                         **breakdown_summary(q.breakdown), "breakdown_flags": list(q.breakdown_flags),
                         "flags": ["suspicious_instruction"] if q.dealer_id in self.flagged else []})
        return rows

    def first_quote(self) -> float | None:
        firm = sorted(self.ledger.firm(), key=lambda q: q.received_at)
        return firm[0].price_total if firm else None

    def _complete(self, t: float) -> None:
        first = self.first_quote()
        best = self.proposal["price_total"]
        s_abs = round(first - best, 2)
        self.log.emit(t, "job.completed", self.winner, {
            "vendor_id": self.winner, "first_quote": first, "best_quote": best, "savings_abs": s_abs,
            "savings_pct": round(100 * s_abs / first, 2), "rounds": self.round,
            "duration_s": int(round((t - self.started_at) * 60)), "trust": dict(self.trust),
            "fee_comparison": self.fee_comparison()})
        self.state = "COMPLETED"
