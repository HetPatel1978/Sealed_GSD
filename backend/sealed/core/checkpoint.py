"""Explicit public engine checkpoints. Runtime callbacks/private vault are excluded."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sealed.core.engine import Engine, Waits
from sealed.core.events import EventLog
from sealed.core.models import Job, MailEvidence, Outbound, Quote

SETS = ("contacted", "nudged", "called", "firm_up_sent", "flagged", "rejected_quotes",
        "audit_observations", "queued_messages")
MAPS = ("price_match_sent", "last_written_at", "reply_addr")


class ReplyContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence: MailEvidence
    subject: str | None


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    version: Literal[1]
    job: Job
    cc: str
    tag: str
    waits: Waits
    state: str
    round: int
    started_at: float
    sets: dict[str, set[str]]
    maps: dict[str, dict]
    sent_keys: set[tuple[str, int, str, str]]
    outbound: dict[str, Outbound]
    reply_context: dict[str, ReplyContext]
    proposal: dict | None
    winner: str | None
    booking_message: str | None
    counters: tuple[int, int, int]
    trust: dict[str, int]
    dealer_emails: dict[str, str | None]
    quotes: list[Quote]
    ledger_counter: int
    log_t0: datetime
    events: list[dict]


def dump(engine: Engine) -> str:
    value = Checkpoint(version=1, job=engine.job, cc=engine.cc, tag=engine.tag, waits=engine.waits,
        state=engine.state, round=engine.round, started_at=engine.started_at,
        sets={key: getattr(engine, key) for key in SETS}, maps={key: getattr(engine, key) for key in MAPS},
        sent_keys=engine.sent_keys, outbound=engine.outbound, reply_context=engine.reply_context,
        proposal=engine.proposal, winner=engine.winner, booking_message=engine.booking_message,
        counters=(engine._n_msg, engine._n_reply, engine._n_prop), trust=engine.trust,
        dealer_emails=engine.ledger.dealer_emails, quotes=engine.ledger.quotes, ledger_counter=engine.ledger._n,
        log_t0=engine.log.t0, events=engine.log.events)
    return value.model_dump_json()


def load(encoded: str, *, transport, checker, qualifier) -> Engine:
    value = Checkpoint.model_validate_json(encoded)
    if set(value.sets) != set(SETS) or set(value.maps) != set(MAPS) or value.log_t0.tzinfo is None:
        raise ValueError("Invalid engine checkpoint.")
    if any(e.get("id") != i or (e.get("job_id"), e.get("run_id"), e.get("mode")) != (
            value.job.id, value.job.run_id, value.job.mode) for i, e in enumerate(value.events, 1)):
        raise ValueError("Invalid checkpoint event scope or sequence.")
    log = EventLog(value.job.id, value.job.run_id, value.job.mode, value.log_t0)
    log.events = value.events
    log._last_ts = value.events[-1]["ts"] if value.events else ""
    engine = Engine(value.job, transport, checker, qualifier, cc=value.cc, tag=value.tag, waits=value.waits, log=log)
    engine.state, engine.round, engine.started_at = value.state, value.round, value.started_at
    for key, item in {**value.sets, **value.maps}.items():
        setattr(engine, key, item)
    engine.sent_keys, engine.outbound = value.sent_keys, value.outbound
    engine.reply_context = {key: {"evidence": item.evidence, "subject": item.subject}
        for key, item in value.reply_context.items()}
    engine.proposal, engine.winner, engine.booking_message = value.proposal, value.winner, value.booking_message
    engine._n_msg, engine._n_reply, engine._n_prop = value.counters
    engine.trust = value.trust
    engine.ledger.dealer_emails, engine.ledger.quotes, engine.ledger._n = value.dealer_emails, value.quotes, value.ledger_counter
    return engine
