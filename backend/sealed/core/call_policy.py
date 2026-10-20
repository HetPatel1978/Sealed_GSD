"""Strict call plans and Fo-reported results. Spoken prices never enter the ledger."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from sealed.core.call_review import review_call


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Plan(Record):
    state: Literal["plan"]
    request_id: str = Field(max_length=64)
    phone: str = Field(max_length=30)
    buyer: str = Field(max_length=80)
    opening: str = Field(max_length=250)
    brief: str = Field(max_length=12000)
    call_started: StrictBool
    followups_scheduled: StrictBool
    max_calls: Literal[1]


class Turn(Record):
    role: Literal["assistant", "dealer"]
    text: str = Field(min_length=1, max_length=2000)


class Report(Record):
    state: Literal["in_progress", "completed"]
    request_id: str = Field(max_length=64)
    phone: str = Field(max_length=30)
    buyer: str = Field(max_length=80)
    opening_used: str | None = Field(max_length=250)
    followups_scheduled: StrictBool
    outcome: Literal["connected", "voicemail", "no_answer", "failed"] | None
    transcript_complete: StrictBool
    transcript: list[Turn] = Field(max_length=80)


def request_id(msg):
    return hashlib.sha256(json.dumps([msg.job_id, msg.run_id, msg.message_id, msg.to,
        msg.call_buyer, msg.body], ensure_ascii=True).encode()).hexdigest()[:32]


def opening(buyer):
    return f"I'm Fo, an AI assistant calling on behalf of {buyer}."


def normalize(text):
    return "".join(char for char in text.casefold() if char.isalnum())


def expected_plan(msg):
    return Plan(state="plan", request_id=request_id(msg), phone=msg.to, buyer=msg.call_buyer,
                opening=opening(msg.call_buyer), brief=msg.body, call_started=False,
                followups_scheduled=False, max_calls=1)


# Fo often wraps the record in a sentence or two, with or without a ```json fence. Exactly one
# JSON object is accepted; the words around it are checked separately for action claims.
# Positive claims in that surrounding text ("nothing dialed" is not one).
DIALED = re.compile(r"\b(?:i(?:'ve| have)?\s+(?:called|dialed|dialled|phoned|placed|started)|calling\s+(?:them|now)|dialing\b)", re.I)
SCHEDULED = re.compile(r"\b(?:i(?:'ve| have)?\s+(?:scheduled|set up)|scheduling\s+(?:the\s+)?retry)\b", re.I)


def _objects(value):
    """Spans of top-level JSON objects embedded in text."""
    decoder, spans, i = json.JSONDecoder(), [], 0
    while (i := value.find("{", i)) != -1:
        try:
            _, end = decoder.raw_decode(value, i)
        except ValueError:
            i += 1
            continue
        spans.append((i, end))
        i = end
    return spans


def _record_text(value):
    spans = _objects(value)
    if len(spans) != 1:
        raise ValueError("invalid_call_record")
    return value[slice(*spans[0])]


def surrounding(text):
    """Fo's own words outside the embedded record, for claim checks."""
    if not isinstance(text, str):
        return ""
    for start, end in reversed(_objects(text)):
        text = text[:start] + " " + text[end:]
    return text


def parse(text, model):
    if not isinstance(text, str) or len(text) > 200_000:
        raise ValueError("invalid_call_record")
    value = _record_text(text.strip())
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_call_field")
            result[key] = item
        return result
    return model.model_validate(json.loads(value, object_pairs_hook=unique))


def plan_matches(msg, text):
    try:
        claimed = DIALED.search(surrounding(text)) or SCHEDULED.search(surrounding(text))
        return not claimed and parse(text, Plan) == expected_plan(msg)
    except ValueError:
        return False


def dial_instruction(msg):
    example = Report(state="in_progress", request_id=request_id(msg), phone=msg.to,
        buyer=msg.call_buyer, opening_used=None, followups_scheduled=False, outcome=None,
        transcript_complete=False, transcript=[]).model_dump_json()
    return (f"Place exactly ONE call using the reviewed plan for request {request_id(msg)}. "
        f"Use exactly this opening: {opening(msg.call_buyer)} "
        "Use the job's buyer name, never the account owner or a name from memory. "
        "If you cannot use that buyer identity, do not dial. Follow only the reviewed brief. "
        "Do not email, retry, schedule reminders, tasks, callbacks, follow-ups or any other action, "
        "including after voicemail, no answer or closed hours. Report back HERE only. "
        "While the call runs, return this JSON envelope with state in_progress: " + example +
        " After it ends, publish the same envelope INLINE here with state completed, the true "
        "outcome (connected, voicemail, no_answer or failed), opening_used as actually spoken "
        "(null if nothing was said), transcript_complete true and the full actual voice transcript "
        "as [{role:assistant|dealer,text:...}]. No invented, shortened or rewritten transcript. "
        "If full evidence is unavailable, use transcript_complete false. Report any identity "
        "deviation or scheduled follow-up honestly. Spoken prices are never acceptance or a written quote.")


def audit_report(msg, report):
    """Return static flags; keep raw transcripts in the private local Fo state only."""
    flags = []
    if report.request_id != request_id(msg) or report.phone != msg.to:
        flags.append("call_scope_mismatch")
    if report.buyer != msg.call_buyer:
        flags.append("buyer_identity_mismatch")
    if report.followups_scheduled:
        flags.append("unrequested_followup")
    expected = normalize(opening(msg.call_buyer))
    if report.opening_used is not None and normalize(report.opening_used) != expected:
        flags.append("buyer_identity_mismatch")
    if report.state == "completed":
        if report.outcome is None or not report.transcript_complete:
            flags.append("call_evidence_missing")
        assistant = [turn.text for turn in report.transcript if turn.role == "assistant"]
        if report.outcome == "connected" or assistant or report.opening_used is not None:
            if (not assistant or report.opening_used is None
                    or expected not in normalize(assistant[0])):
                flags.append("buyer_identity_unverified")
        transcript_flags = review_call([turn.model_dump() for turn in report.transcript], msg.body)
        flags.extend(item["flag"] for item in transcript_flags)
        for text in assistant:
            for match in re.finditer(r"\b(?:i'm|i am|this is)\s+([a-z][a-z '\u2019-]{0,80})['\u2019]s\s+(?:ai\s+)?assistant\b", text, re.I):
                if normalize(match[1]) != normalize(msg.call_buyer):
                    flags.append("buyer_identity_mismatch")
    return sorted(set(flags))
