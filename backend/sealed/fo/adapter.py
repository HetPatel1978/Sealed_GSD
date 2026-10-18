"""Fo adapter: the live `Transport` for the engine. Sends emails and calls through Fo, one turn at a time.

Rules (docs/architecture.md §7, spikes 1a-1d):
- One Fo conversation per dealer: the first message opens a new conversation; later ones reuse its id.
- Serial queue: one ask_fo turn at a time for the whole account (a lock).
- Never resend. A message id is recorded as `pending` BEFORE ask_fo; on a timeout or "working" we poll
  get_fo_reply; a message already pending/sent/unclear is never sent again (a human checks it).
- Fo may answer with a preview/confirmation question: the guard checks it (approve_preview) and we answer
  "yes" only if it passes; otherwise we tell Fo not to send.
- Fo may refuse (spike 1b) or reply ambiguously: not counted as sent; raises needs_human.
- If Fo mentions payment, purchase or an order, we stop the turn and flag it.
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from sealed.core.engine import SendResult
from sealed.core.models import Outbound
from sealed.fo.client import FoClient, FoTimeout

POLL_LIMIT_S = 600
CONFIRM_YES = "Yes, send it exactly as written."
CONFIRM_YES_CALL = "Yes, place the call exactly as described."
CONFIRM_NO = "No, please don't send or do anything for this request. I'll follow up."

DONE = re.compile(r"^\s*(sent|done|call'?s? (is )?(dispatched|placed|done)|calling|dialing|it'?s on its way|"
                  r"email (is )?(sent|on its way)|i('ve| have) sent|i('ve| have) (placed|started) the call)", re.I)
REFUSED = re.compile(r"\b(stopped|can'?t|cannot|won'?t|unable|not able|refuse|declin(e|ed))\b", re.I)
QUESTION = re.compile(r"\?\s*$|\b(good to send|want me to|should i|shall i|ok to send|okay to send|confirm|"
                      r"ready to send|go ahead)\b", re.I)
MONEY_ACTION = re.compile(r"\b(payment|paid|purchas\w*|charged|checkout|order (placed|confirmed)|deposit)\b", re.I)

# One ask_fo turn at a time for the whole account: sends, calls and dealer discovery all take this lock.
FO_TURN_LOCK = threading.Lock()


def classify(text: str) -> str:
    """'sent' | 'refused' | 'question' | 'unclear'. Conservative: anything not clearly sent isn't sent."""
    t = text.strip()
    if DONE.search(t):
        return "sent"
    if REFUSED.search(t):
        return "refused"
    if QUESTION.search(t):
        return "question"
    return "unclear"


class Store:
    """Persists dealer -> conversation id and message id -> status, so a restart never resends."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"conversations": {}, "messages": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def conversation(self, dealer_id: str) -> str | None:
        return self.data["conversations"].get(dealer_id)

    def set_conversation(self, dealer_id: str, cid: str) -> None:
        self.data["conversations"][dealer_id] = cid
        self.save()

    def status(self, message_id: str) -> str | None:
        return self.data["messages"].get(message_id, {}).get("status")

    def set_status(self, message_id: str, status: str, **extra) -> None:
        self.data["messages"].setdefault(message_id, {}).update({"status": status, **extra})
        self.save()


class FoTransport:
    def __init__(self, client: FoClient, store_path: Path, poll_limit_s: float = POLL_LIMIT_S,
                 clock: Callable[[], float] = time.monotonic, wait_seconds: int = 40,
                 lock: threading.Lock = FO_TURN_LOCK):
        self.client, self.store = client, Store(store_path)
        self.poll_limit_s, self.clock, self.wait_seconds = poll_limit_s, clock, wait_seconds
        self._lock = lock
        self.transcript: list[dict] = []     # every raw Fo result, for logs and debugging

    def send(self, now: float, msg: Outbound, approve_preview: Callable[[str], bool]) -> SendResult:
        prior = self.store.status(msg.message_id)
        if prior is not None:
            return SendResult(prior == "sent", now, errors=[{
                "code": "duplicate_suppressed", "retryable": False, "resent": False,
                "message": f"Message {msg.message_id} was already {prior}; not sent again."}],
                reason_code=None if prior == "sent" else "send_unclear",
                note=None if prior == "sent" else "An earlier attempt is still unresolved; check Gmail before retrying.")
        with self._lock:
            t0 = self.clock()
            self.store.set_status(msg.message_id, "pending", dealer_id=msg.dealer_id, channel=msg.channel)
            errors: list[dict] = []
            cid = self.store.conversation(msg.dealer_id)
            res = self._ask(msg.fo_instruction, cid, errors)
            if res.get("conversationId") and not cid:
                cid = res["conversationId"]
                self.store.set_conversation(msg.dealer_id, cid)
            res = self._settle(res, cid, errors)
            outcome, text = self._outcome(res)

            if outcome == "question":
                if approve_preview(text):
                    yes = CONFIRM_YES_CALL if msg.channel == "call" else CONFIRM_YES
                    res = self._settle(self._ask(yes, cid, errors), cid, errors)
                    outcome, text = self._outcome(res)
                    if outcome == "question":
                        outcome = "unclear"
                else:
                    self._settle(self._ask(CONFIRM_NO, cid, errors), cid, errors)
                    outcome = "preview_rejected"

            if MONEY_ACTION.search(text or ""):
                errors.append({"code": "fo_mentions_payment", "retryable": False, "resent": False,
                               "message": "Fo's reply mentions a payment, purchase or order. Stopped; please review."})
                outcome = "stopped" if outcome != "sent" else "sent_flagged"

            sent_at = now + (self.clock() - t0) / 60
            status = {"sent": "sent", "sent_flagged": "sent"}.get(outcome, outcome)
            self.store.set_status(msg.message_id, status, conversation=cid, fo_reply=text[:500] if text else None)
            if outcome in ("sent", "sent_flagged"):
                # The CC copy is fetched from Gmail by reply ingestion (step 3) for the post-send audit.
                return SendResult(True, sent_at, sent_copy=None, errors=errors,
                                  reason_code="payment_mention" if outcome == "sent_flagged" else None,
                                  note=text)
            reason = {"refused": "fo_refused", "preview_rejected": "audit_mismatch", "failed": "fo_failed",
                      "stopped": "payment_mention", "still_working": "send_unclear"}.get(outcome, "send_unclear")
            return SendResult(False, sent_at, errors=errors, reason_code=reason, note=text)

    # ---- helpers -------------------------------------------------------------------------------------
    def _ask(self, message: str, cid: str | None, errors: list[dict]) -> dict:
        return ask_turn(self.client, message, cid, errors, self.transcript)

    def _settle(self, res: dict, cid: str | None, errors: list[dict]) -> dict:
        return settle_turn(self.client, res, cid, errors, self.transcript,
                           self.poll_limit_s, self.clock, self.wait_seconds)

    @staticmethod
    def _outcome(res: dict) -> tuple[str, str]:
        status, text = res.get("status"), res.get("response") or res.get("progress") or ""
        if status == "failed":
            return "failed", text
        if status == "still_working":
            return "still_working", text
        return classify(text), text


# ---- one Fo turn (shared with sealed.discovery.fo_finder) ----------------------------------------------
def ask_turn(client: FoClient, message: str, cid: str | None, errors: list[dict], transcript: list[dict]) -> dict:
    """ask_fo once. A timeout is recorded and treated as `working` (poll next, never resend)."""
    try:
        res = client.ask(message, cid)
    except FoTimeout as e:
        errors.append({"code": "fo_turn_timeout", "retryable": True, "resent": False,
                       "message": f"{e}. Polling get_fo_reply; not resent."})
        res = {"status": "working", "conversationId": cid}
    transcript.append({"tool": "ask_fo", "message": message, "result": res})
    return res


def settle_turn(client: FoClient, res: dict, cid: str | None, errors: list[dict], transcript: list[dict],
                poll_limit_s: float, clock: Callable[[], float], wait_seconds: int) -> dict:
    """Polls get_fo_reply while Fo is `working`, up to poll_limit_s; then returns status `still_working`."""
    start = clock()
    cid = res.get("conversationId") or cid
    while res.get("status") == "working":
        if clock() - start > poll_limit_s:
            errors.append({"code": "fo_still_working", "retryable": False, "resent": False,
                           "message": "Fo is still working after the poll limit. Not resent; please check."})
            return {"status": "still_working", "conversationId": cid}
        try:
            res = client.reply(cid, wait_seconds)
        except FoTimeout:
            continue
        transcript.append({"tool": "get_fo_reply", "result": res})
    return res
