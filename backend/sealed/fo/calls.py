"""Checked call plans and durable read-only observation of one authorized call."""

import re
import time
from datetime import datetime, timezone

from sealed.core.call_policy import Plan, Report, audit_report, dial_instruction, parse, plan_matches, request_id
from sealed.core.engine import SendResult
from sealed.core.guard import payload_intact
from sealed.core.models import outbound_fingerprint
from sealed.fo.adapter import Store, _TURN_LOCK
from sealed.fo.client import FoTimeout

CALL_RESULT_TIMEOUT_S = 1800


class CallTransport:
    def __init__(self, client, path, *, clock=time.time):
        self.client, self.store, self.clock = client, Store(path), clock

    @staticmethod
    def _failure(now, code="send_unclear"):
        return SendResult(False, now, reason_code=code)

    @staticmethod
    def _pending(now):
        return SendResult(False, now, pending_call=True)

    def _cid(self, msg, result, cid):
        candidate = result.get("conversationId")
        if candidate:
            if not isinstance(candidate, str) or (cid and candidate != cid):
                raise ValueError("call_conversation_mismatch")
            if not cid:
                cid = candidate
                self.store.set_conversation(msg, cid)
        return cid

    def send(self, now, msg, approve_preview):
        if not msg.job_id or not msg.run_id or not payload_intact(msg):
            return self._failure(now, "guard_blocked")
        key, fingerprint = Store.key(msg.job_id, msg.run_id, msg.message_id), outbound_fingerprint(msg)
        with _TURN_LOCK:
            self.store.reload()
            prior = self.store.message(key)
            if prior:
                # An existing identity is never asked again, including a plan that
                # crashed before permission. Only poll_call observes known calls.
                if prior.get("fingerprint") != fingerprint:
                    return self._failure(now)
                if prior.get("status") == "call_pending":
                    return self._pending(now)
                if prior.get("status") == "sent" and prior.get("report"):
                    return self._result(now, msg, Report.model_validate(prior["report"]))
                return self._failure(now, prior.get("reason_code") or "send_unclear")
            if self.store.data.get("active"):
                return self._failure(now)
            cid, dial_reserved = self.store.conversation(msg), False
            try:
                self.store.set_status(key, "pending", active=True, phase="plan_pending", fingerprint=fingerprint,
                    dealer_id=msg.dealer_id, channel="call", contract="sealed-call-v1",
                    attempted_at=datetime.now(timezone.utc).isoformat())
                result = self.client.ask(msg.fo_instruction, cid)
                cid = self._cid(msg, result, cid)
                if not cid or result.get("status") != "complete":
                    raise ValueError("call_plan_unverified")
                text = result.get("response", "")
                plan = parse(text, Plan)
                if not plan_matches(msg, text) or not approve_preview(text):
                    # A valid plan explicitly states no call/task has started.
                    safe = plan.call_started is False and plan.followups_scheduled is False
                    self.store.set_status(key, "rejected" if safe else "unclear", active=not safe,
                        conversation=cid, reason_code="call_plan_rejected" if safe else "send_unclear")
                    return self._failure(now, "call_plan_rejected" if safe else "send_unclear")
                self.store.set_status(key, "call_pending", active=True, phase="dial_pending", conversation=cid,
                    plan=plan.model_dump(), deadline=self.clock() + CALL_RESULT_TIMEOUT_S,
                    dial_at=datetime.now(timezone.utc).isoformat())
                dial_reserved = True  # durable BEFORE the sole permission to dial
                try:
                    result = self.client.ask(dial_instruction(msg), cid)
                except FoTimeout:
                    return self._pending(now)
                try:
                    self._cid(msg, result, cid)
                except ValueError:
                    self.store.set_status(key, "unclear", active=True, reason_code="call_review")
                    return self._failure(now, "call_review")
                return self._observe(now, msg, key, result)
            except Exception:
                if dial_reserved:
                    # The call may already be in progress; read its conversation
                    # on the next cycle instead of guessing/repeating permission.
                    return self._pending(now)
                try:
                    self.store.set_status(key, "unclear", active=True, conversation=cid, reason_code="send_unclear")
                except Exception:
                    pass
                return self._failure(now)

    def _result(self, now, msg, report):
        flags = audit_report(msg, report)
        identity_status = "needs_review"
        if report.transcript_complete and not any("identity" in flag for flag in flags):
            identity_status = "verified" if report.opening_used is not None else "not_spoken"
        summary = {"outcome": report.outcome, "identity_verified": identity_status == "verified",
                   "identity_status": identity_status,
                   "followups_clear": not report.followups_scheduled,
                   "transcript_complete": report.transcript_complete, "flags": flags}
        return SendResult(True, now, reason_code="call_review" if flags else None, call_report=summary)

    def _observe(self, now, msg, key, result):
        if result.get("status") == "working":
            return self._pending(now)
        text = result.get("response", "")
        try:
            report = parse(text, Report)
        except ValueError:
            # A natural acknowledgement such as 'On it, calling ...' is progress,
            # not proof that a call ended. Await a bounded, scoped final report.
            if result.get("status") == "complete" and re.search(
                    r"\b(?:i(?:'ve| have)?\s+(?:scheduled|set up)|scheduling\s+(?:the\s+)?retry)\b", text, re.I):
                self.store.set_status(key, "unclear", active=True, reason_code="call_review")
                return self._failure(now, "call_review")
            return self._pending(now)
        flags = audit_report(msg, report)
        if "call_scope_mismatch" in flags:
            self.store.set_status(key, "unclear", active=True, reason_code="call_review", report=report.model_dump())
            return self._failure(now, "call_review")
        if report.state == "in_progress":
            if flags:
                self.store.set_status(key, "unclear", active=True, reason_code="call_review", report=report.model_dump())
                return self._failure(now, "call_review")
            return self._pending(now)
        self.store.set_status(key, "unclear" if flags else "sent", active=bool(flags),
            phase="completed", reason_code="call_review" if flags else None, report=report.model_dump())
        return self._result(now, msg, report)

    def poll_call(self, now, msg):
        """Read existing conversation only. Never ask, confirm, cancel or dial."""
        key = Store.key(msg.job_id, msg.run_id, msg.message_id)
        with _TURN_LOCK:
            self.store.reload()
            record = self.store.message(key)
            if not record or record.get("fingerprint") != outbound_fingerprint(msg):
                return self._failure(now)
            if record.get("status") == "sent" and record.get("report"):
                return self._result(now, msg, Report.model_validate(record["report"]))
            if record.get("status") == "unclear":
                if record.get("phase") == "completed" and record.get("report"):
                    return self._result(now, msg, Report.model_validate(record["report"]))
                return self._failure(now, record.get("reason_code") or "send_unclear")
            if record.get("status") != "call_pending" or record.get("phase") != "dial_pending":
                return self._failure(now)
            cid = record.get("conversation")
            if (not cid or cid != self.store.conversation(msg)
                    or self.store.data.get("active") != key):
                return self._failure(now)
            if self.clock() >= record.get("deadline", 0):
                self.store.set_status(key, "unclear", active=True, reason_code="call_result_missing")
                return self._failure(now, "call_result_missing")
            try:
                result = self.client.conversation(cid)
                if result.get("conversation", {}).get("conversationId") != cid:
                    raise ValueError("call_conversation_mismatch")
                turns = result.get("turns", [])
                if not isinstance(turns, list):
                    raise ValueError("invalid_call_conversation")
                # Only assistant reports for this exact request are accepted.
                # Examine the newest structured report, not a previous dealer call.
                for turn in reversed(turns):
                    if not isinstance(turn, dict) or turn.get("role") != "assistant":
                        continue
                    try:
                        report = parse(turn.get("text", ""), Report)
                    except ValueError:
                        at = turn.get("at")
                        if isinstance(at, str):
                            try:
                                fresh = datetime.fromisoformat(at) >= datetime.fromisoformat(record["dial_at"])
                            except (ValueError, KeyError, TypeError):
                                fresh = False
                            if fresh:
                                observed = self._observe(now, msg, key, {"status": "complete", "response": turn.get("text", "")})
                                if not observed.pending_call:
                                    return observed
                        continue
                    if report.request_id != request_id(msg):
                        continue
                    return self._observe(now, msg, key, {"status": "complete", "response": report.model_dump_json()})
                return self._pending(now)
            except Exception:
                # Read failures do not issue new permission or erase the claim.
                return self._pending(now)
