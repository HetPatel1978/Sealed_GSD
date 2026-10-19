"""Fo adapter: the live `Transport` for the engine. Sends emails and calls through Fo, one turn at a time.

Rules (docs/architecture.md §7, spikes 1a-1d):
- One Fo conversation per (job, run, dealer); later messages reuse its id, including IDs learned while polling.
- Serial queue: one ask_fo turn at a time for the account, across all transports in this process.
- Never resend. A message id is recorded as `pending` BEFORE ask_fo; on a timeout or "working" we poll
  get_fo_reply; a message already pending/sent/unclear is never sent again (a human checks it).
- Brief-mode email asks Fo to compose a full draft before sending. The guard checks routing, prices and
  constraints; the draft is durable before confirmation. A send without a checked draft raises review.
- Legacy exact-email and call previews retain their original checks; nothing is automatically resent.
- Fo may refuse (spike 1b) or reply ambiguously: not counted as sent; raises needs_human.
- If Fo mentions payment, purchase or an order, we stop the turn and flag it.

Use one process and one state file per Fo account. Unresolved or flagged turns keep an active marker
across restarts and require review before another send. See docs/fo_adapter.md for live acceptance.
"""

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Callable

from sealed.core.engine import SendResult
from sealed.core.models import EmailAudit, EmailDraft, Outbound, outbound_fingerprint
from sealed.core.email_policy import EXTRA_ACTION, parse_draft, payment_text, review_rules
from sealed.core.guard import payload_intact
from sealed.fo.client import FoClient, FoTimeout

POLL_LIMIT_S = 600
# One adapter process and one state file per Fo account. All instances in this process share the queue.
_TURN_LOCK = threading.RLock()
CONFIRM_YES = "Yes, send it exactly as written."
CONFIRM_YES_CALL = "Yes, place the call exactly as described."
CONFIRM_BRIEF = ("Yes, send that reviewed draft once with exactly the displayed recipient, CC, subject and body. "
                 "Do not send any other messages, arrange payments, call or schedule follow-ups.")
CONFIRM_NO = "No, please don't send or do anything for this request. I'll follow up."

DONE = re.compile(r"^\s*(sent(?=$|[.!,]|\s+(to|from|via|the|it)\b)|done(?=$|[.!])|"
                  r"call'?s? (is )?(dispatched|placed|done)\b|calling\b|dialing\b|it'?s on its way\b|"
                  r"email (is )?(sent|on its way)\b|i('ve| have) sent\b|i('ve| have) (placed|started) the call\b)", re.I)
REFUSED = re.compile(r"\b(stopped|can'?t|cannot|won'?t|unable|not able|refuse|declin(e|ed))\b", re.I)
QUESTION = re.compile(r"\?|\b(good to send|want me to|should i|shall i|ok to send|okay to send|confirm|"
                      r"ready to send|go ahead)\b", re.I)
MONEY_ACTION = re.compile(r"\b(payment|paid|purchas\w*|charged|checkout|order (placed|confirmed)|deposit)\b", re.I)


def classify(text: str) -> str:
    """'sent' | 'refused' | 'question' | 'unclear'. Conservative: anything not clearly sent isn't sent."""
    t = text.strip()
    first_sentence_is_question = re.match(r"^[^.!?]*\?", t) is not None
    draft_done = re.match(r"done(?=$|[.!])", t, re.I) and QUESTION.search(t)
    if DONE.search(t) and not first_sentence_is_question and not draft_done:
        return "sent"
    if REFUSED.search(t):
        return "refused"
    if QUESTION.search(t):
        return "question"
    return "unclear"


class Store:
    """Job/run-scoped state. Writes replace the complete JSON file atomically.

    Callers hold _TURN_LOCK and reload before reserving a send. Unscoped nonempty legacy files fail
    closed: guessing a migration could send a message that the old adapter already delivered.
    """

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.reload()

    def reload(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Invalid Fo state file; review it before sending.")
        if data.get("version") not in (None, 2):
            raise ValueError("Unsupported Fo state version; review it before sending.")
        if data.get("version") is None and (data.get("messages") or data.get("conversations")):
            raise ValueError("Legacy unscoped Fo state requires manual review before sending.")
        if any(not isinstance(data.get(k, {}), dict) for k in ("messages", "conversations")):
            raise ValueError("Invalid Fo state file; review it before sending.")
        self.data = {"conversations": {}, "messages": {}, "active": None, **data, "version": 2}
        if any(not isinstance(v, dict) for v in self.data["messages"].values()):
            raise ValueError("Invalid Fo message state; review it before sending.")

    @staticmethod
    def key(*parts: str) -> str:
        return json.dumps(parts, separators=(",", ":"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def conversation(self, msg: Outbound) -> str | None:
        return self.data["conversations"].get(self.key(msg.job_id, msg.run_id, msg.dealer_id))

    def set_conversation(self, msg: Outbound, cid: str) -> None:
        self.data["conversations"][self.key(msg.job_id, msg.run_id, msg.dealer_id)] = cid
        self.save()

    def message(self, key: str) -> dict | None:
        return self.data["messages"].get(key)

    def set_status(self, key: str, status: str, *, active: bool = False, **extra) -> None:
        self.data["messages"].setdefault(key, {}).update({"status": status, **extra})
        self.data["active"] = key if active else None
        self.save()


class FoTransport:
    def __init__(self, client: FoClient, store_path: Path, poll_limit_s: float = POLL_LIMIT_S,
                 clock: Callable[[], float] = time.monotonic, wait_seconds: int = 40,
                 sleep: Callable[[float], None] = time.sleep,
                 sent_auditor: Callable[[Outbound, str], EmailAudit] | None = None):
        if not isfinite(poll_limit_s) or poll_limit_s <= 0 or not 0 <= wait_seconds <= 40:
            raise ValueError("Use a positive poll limit and wait_seconds between 0 and 40.")
        self.client, self.store = client, Store(store_path)
        self.poll_limit_s, self.clock, self.wait_seconds = poll_limit_s, clock, wait_seconds
        self.sleep = sleep
        self.sent_auditor = sent_auditor
        self.transcript: list[dict] = []     # every raw Fo result, for logs and debugging

    def send(self, now: float, msg: Outbound, approve_preview: Callable[[str], bool]) -> SendResult:
        if msg.channel == "call" and msg.call_buyer is not None:
            from sealed.fo.calls import CallTransport
            return CallTransport(self.client, self.store.path).send(now, msg, approve_preview)
        if not msg.job_id or not msg.run_id:
            raise ValueError("A live Fo message requires job_id and run_id.")
        if msg.email_brief is not None and not payload_intact(msg):
            return SendResult(False, now, reason_code="guard_blocked")
        key = Store.key(msg.job_id, msg.run_id, msg.message_id)
        fingerprint = outbound_fingerprint(msg)
        with _TURN_LOCK:
            # A second transport instance may have changed this file while this one waited.
            self.store.reload()
            prior = self.store.message(key)
            if prior is not None:
                same = prior.get("fingerprint") == fingerprint
                return SendResult(same and prior["status"] == "sent", now, errors=[{
                    "code": "duplicate_suppressed" if same else "message_id_conflict",
                    "retryable": False, "resent": False,
                    "message": "This message was already attempted; not sent again." if same else
                               "This message ID was used for a different payload; nothing was sent.",
                }], reason_code=(prior.get("reason_code") if same and prior["status"] == "sent" else "send_unclear"),
                    note=prior.get("fo_reply"),
                    draft=EmailDraft(**prior["draft"]) if same and prior.get("draft") else None,
                    audit=EmailAudit.from_dict(prior["audit"]) if same and prior.get("audit") else None)
            if self.store.data["active"]:
                return SendResult(False, now, reason_code="send_unclear", errors=[{
                    "code": "fo_turn_unresolved", "retryable": False, "resent": False,
                    "message": "An earlier Fo turn is unresolved. Review it before starting another turn.",
                }])
            t0 = self.clock()
            errors: list[dict] = []
            cid = self.store.conversation(msg)
            reserved = False
            reviewed_draft = None
            attempted_at = datetime.now(timezone.utc).isoformat()
            try:
                # Reservation and active-turn marker are durable before the first non-idempotent call.
                self.store.set_status(key, "pending", active=True, fingerprint=fingerprint,
                                      dealer_id=msg.dealer_id, channel=msg.channel, attempted_at=attempted_at)
                reserved = True

                def remember(res: dict) -> str | None:
                    nonlocal cid
                    candidate = res.get("conversationId")
                    if candidate:
                        if not isinstance(candidate, str) or (cid and cid != candidate):
                            raise ValueError("Fo returned a different conversation.")
                        if cid != candidate:
                            cid = candidate
                            self.store.set_conversation(msg, cid)
                    return cid

                def turn(message: str) -> dict:
                    return self._settle(self._ask(message, cid, errors), cid, errors, remember,
                                        deadline=t0 + self.poll_limit_s)

                res = turn(msg.fo_instruction)
                outcome, text = self._outcome(res)
                if msg.email_brief is not None and parse_draft(text) is not None:
                    outcome = "question"
                reason_override = None
                risky = self._money_mention(text, msg)
                if outcome == "question":
                    approved = approve_preview(text)
                    candidate_draft = parse_draft(text) if msg.email_brief is not None else None
                    if msg.email_brief is not None:
                        approved = approved and candidate_draft is not None and all(
                            rule["passed"] for rule in review_rules(msg, candidate_draft))
                    if not cid:
                        # A timeout may lose the ID. Polling can discover it; a new ask must never guess it.
                        outcome = "unclear"
                    elif approved and not risky:
                        if candidate_draft is not None:
                            # The exact reviewed artifact is durable BEFORE permission to send.
                            self.store.set_status(key, "pending", active=True, draft=asdict(candidate_draft))
                            reviewed_draft = candidate_draft
                        confirmation = CONFIRM_BRIEF if msg.email_brief is not None else (
                            CONFIRM_YES_CALL if msg.channel == "call" else CONFIRM_YES)
                        res = turn(confirmation)
                        outcome, text = self._outcome(res)
                        if outcome == "question":
                            outcome = "unclear"
                    else:
                        res = turn(CONFIRM_NO)
                        cancelled, cancellation = self._outcome(res)
                        if cancelled == "sent":
                            outcome, text = "sent", cancellation
                            reason_override = "payment_mention" if risky else "audit_mismatch"
                        elif cancelled == "refused":
                            outcome = "stopped" if risky else "preview_rejected"
                        else:
                            outcome = "still_working" if cancelled == "still_working" else "unclear"
                if outcome == "sent" and not cid:
                    reason_override = "send_unclear"
                if outcome == "sent" and msg.email_brief is not None and reviewed_draft is None and reason_override is None:
                    reason_override = "audit_mismatch"
                    errors.append({"code": "fo_sent_without_reviewed_draft", "retryable": False, "resent": False,
                                   "message": "Fo reported sending before draft review. Not resent; check Gmail."})
                if outcome == "sent" and msg.email_brief is not None and EXTRA_ACTION.search(text):
                    reason_override = "audit_mismatch"
                    errors.append({"code": "fo_action_outside_brief", "retryable": False, "resent": False,
                                   "message": "Fo reported activity outside the single authorized email; review the conversation."})
                if risky or self._money_mention(text, msg):
                    errors.append({"code": "fo_mentions_payment", "retryable": False, "resent": False,
                                   "message": "Fo mentioned a payment, purchase or order. Stopped; please review."})
                    reason_override = "payment_mention"
                    if outcome not in ("sent", "unclear", "still_working"):
                        outcome = "stopped"

                sent = outcome == "sent"
                reason = reason_override if sent else reason_override or {
                    "refused": "fo_refused", "preview_rejected": "audit_mismatch", "failed": "fo_failed",
                    "stopped": "payment_mention",
                }.get(outcome, "send_unclear")
                audit = None
                if sent and msg.channel == "email" and self.sent_auditor is not None:
                    # Keep the account locked and the sent reservation active while
                    # reading Gmail. A crash here preserves "sent", never "retry".
                    self.store.set_status(key, "sent", active=True, conversation=cid,
                                          reason_code=reason or "send_unclear", fo_reply=text[:500], audit_pending=True)
                    try:
                        audit = self.sent_auditor(msg, attempted_at)
                        if not audit.validates(msg):
                            raise ValueError("Wrong outbound audit result")
                        if not audit.matched:
                            reason = reason or ("send_unclear" if audit.state == "pending" else "audit_mismatch")
                    except Exception:
                        audit = None
                        reason = reason or "audit_mismatch"
                        errors.append({"code": "gmail_audit_failed", "retryable": True, "resent": False,
                                       "message": "The email was reported sent, but Gmail auditing failed. Paused for review; do not resend."})
                self.store.set_status(key, "sent" if sent else outcome,
                                      active=outcome in ("still_working", "unclear") or reason_override is not None or (sent and reason is not None),
                                      conversation=cid,
                                      reason_code=reason, fo_reply=text[:500],
                                      **({"audit": asdict(audit), "audit_pending": audit.state == "pending"} if audit else {}))
                # A configured Gmail auditor supplies actual evidence, never a Fo self-report.
                return SendResult(sent, now + (self.clock() - t0) / 60, errors=errors,
                                  reason_code=reason, note=text, draft=reviewed_draft, audit=audit)
            except Exception:
                # Never echo raw SDK/HTTP exceptions: they can include tokens or request contents.
                if reserved:
                    try:
                        self.store.set_status(key, "unclear", active=True, conversation=cid,
                                              reason_code="send_unclear")
                    except Exception:
                        pass  # The durable pending reservation still suppresses retries after restart.
                errors.append({"code": "fo_integration_error", "retryable": False, "resent": False,
                               "message": "Fo or its state store failed. No automatic resend; please review."})
                return SendResult(False, now + (self.clock() - t0) / 60, errors=errors,
                                  reason_code="send_unclear" if reserved else "integration_down")

    # ---- helpers -------------------------------------------------------------------------------------
    def _ask(self, message: str, cid: str | None, errors: list[dict]) -> dict:
        try:
            res = self.client.ask(message, cid)
        except FoTimeout:
            errors.append({"code": "fo_turn_timeout", "retryable": True, "resent": False,
                           "message": "Fo timed out. Polling get_fo_reply; not resent."})
            res = {"status": "working", "conversationId": cid}
        self.transcript.append({"tool": "ask_fo", "message": message, "result": res})
        return res

    def _settle(self, res: dict, cid: str | None, errors: list[dict], remember, *, deadline: float) -> dict:
        while True:
            cid = remember(res)
            if res.get("status") != "working":
                return res
            if self.clock() >= deadline:
                errors.append({"code": "fo_still_working", "retryable": False, "resent": False,
                               "message": "Fo is still working after the poll limit. Not resent; please check."})
                return {"status": "still_working", "conversationId": cid}
            try:
                res = self.client.reply(cid, self.wait_seconds)
            except FoTimeout:
                continue
            self.transcript.append({"tool": "get_fo_reply", "result": res})
            if res.get("status") == "working":
                self.sleep(min(1.0, max(0.0, deadline - self.clock())))

    @staticmethod
    def _money_mention(text: str, msg: Outbound) -> bool:
        if msg.email_brief is not None:
            return bool(MONEY_ACTION.search(payment_text(text, booking=msg.booking_approved)))
        # A verbatim approved booking preview contains "purchase paperwork" and an explicit no-payment line.
        # Only that exact known body is exempt; added payment instructions still stop before confirmation.
        normalized = re.sub(r"\s+", " ", text).strip()
        body = re.sub(r"\s+", " ", msg.body).strip()
        return bool(MONEY_ACTION.search(normalized.replace(body, "")))

    @staticmethod
    def _outcome(res: dict) -> tuple[str, str]:
        status, text = res.get("status"), res.get("response") or res.get("progress") or ""
        if status == "failed":
            return "failed", text
        if status == "still_working":
            return "still_working", text
        if status != "complete":
            return "unclear", text
        return classify(text), text
