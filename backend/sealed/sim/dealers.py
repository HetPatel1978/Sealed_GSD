"""Scripted stand-in dealers + a sim transport that stands in for Fo.

A script maps (message kind, channel) -> list of responses. Each response arrives `delay` minutes after the
message is sent, as a written email or as Fo's summary of a call. Parsed fields are given directly (in live
mode the quote parser produces them in step 3).
"""

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Callable

from sealed.core.engine import SendResult
from sealed.core.models import Dealer, Inbound, Outbound, ParsedReply


@dataclass(frozen=True)
class Response:
    delay: float
    source: str                     # "email" | "call"
    text: str
    parsed: ParsedReply
    turns: tuple[dict, ...] = ()      # call transcript, for call responses
    from_addr: str | None = None      # sender of an email reply (None = the dealer's email on file)


@dataclass
class SimDealer:
    dealer: Dealer
    script: dict[tuple[str, str], list[Response]] = field(default_factory=dict)


class Scheduler:
    """Min-heap of (time, seq, callback)."""

    def __init__(self):
        self._heap: list = []
        self._seq = itertools.count()

    def at(self, t: float, fn: Callable[[float], None]) -> None:
        heapq.heappush(self._heap, (t, next(self._seq), fn))

    def pop(self):
        return heapq.heappop(self._heap) if self._heap else None


class SimTransport:
    """Stands in for Fo: one turn at a time, echoes a preview on first contact and on bookings,
    delivers the exact text (sent copy == body), and schedules the dealer's scripted responses."""

    def __init__(self, dealers: dict[str, SimDealer], scheduler: Scheduler, deliver: Callable[[float, Inbound], None],
                 turn_minutes: float = 1.0, slow_turns: set[str] = frozenset()):
        self.dealers, self.scheduler, self.deliver = dealers, scheduler, deliver
        self.turn_minutes, self.slow_turns = turn_minutes, set(slow_turns)
        self.busy_until = 0.0
        self.seen: set[str] = set()

    def send(self, now: float, msg: Outbound, approve_preview) -> SendResult:
        start = max(now, self.busy_until)
        errors = []
        first = msg.dealer_id not in self.seen
        if (first and msg.channel == "email") or msg.kind == "booking":
            if not approve_preview(msg.body):
                self.busy_until = start + self.turn_minutes
                return SendResult(False, self.busy_until)
        turn = self.turn_minutes
        if msg.dealer_id in self.slow_turns and first:
            turn += 1.5
            errors.append({"code": "fo_turn_timeout",
                           "message": f"Fo is still working on the {msg.channel} after 90s. Still polling; not resent.",
                           "retryable": True, "resent": False})
        sent_at = start + turn
        self.busy_until = sent_at
        self.seen.add(msg.dealer_id)
        sim = self.dealers[msg.dealer_id]
        for r in sim.script.get((msg.kind, msg.channel), []):
            inbound = Inbound(msg.dealer_id, r.source, r.text, r.parsed, in_reply_to=msg.message_id, turns=r.turns,
                              from_addr=r.from_addr)
            self.scheduler.at(sent_at + r.delay, lambda t, i=inbound: self.deliver(t, i))
        return SendResult(True, sent_at, sent_copy=msg.body if msg.channel == "email" else None, errors=errors)
