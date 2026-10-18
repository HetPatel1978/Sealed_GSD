"""Plain data types shared by the core. Note: Job has no ceiling field, on purpose (see sealed.private.vault)."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dealer:
    """A dealer needs an email, a phone, or both. Phone-only dealers get the round-1 request as a call and are
    emailed later only at the address their first written reply came from."""
    id: str
    name: str
    email: str | None
    phone: str | None = None
    mode: str = "sim"
    source: str = "manual"                  # "manual" (typed in) | "discovery" (POST /dealers/discover)
    source_urls: tuple[str, ...] = ()       # pages the contact details were found on (discovery only)

    def __post_init__(self):
        if not self.email and not self.phone:
            raise ValueError(f"dealer {self.id} needs an email or a phone")
        if self.source not in ("manual", "discovery"):
            raise ValueError(f"dealer {self.id}: unknown source {self.source!r}")


@dataclass(frozen=True)
class Job:
    id: str
    item: str                 # e.g. "2026 Honda Civic Sport"
    details: str              # exact model / trim / color
    zip: str
    buyer: str                # first name used in messages, e.g. "Arjun"
    timeline: str             # true timeline phrase, e.g. "within the next four weeks"
    deadline: str             # ISO date; quotes available after this don't qualify
    target: float             # public; the only price we ever ask for
    dealers: tuple[Dealer, ...]
    currency: str = "USD"
    max_rounds: int = 2
    run_id: str = "run_01"
    mode: str = "sim"

    def dealer(self, dealer_id: str) -> Dealer:
        return next(d for d in self.dealers if d.id == dealer_id)


@dataclass(frozen=True)
class ParsedReply:
    """What the quote parser (LLM in live mode, scripted in sim) extracts. Code decides firmness, not the parser."""
    price_total: float | None = None
    out_the_door: bool = False
    meets_spec: bool = True
    available: str | None = None      # ISO date the car is available
    hedge_words: tuple[str, ...] = ()
    declined: bool = False
    suspicious: bool = False
    confirms_booking: bool = False
    numbers_grounded: bool = True
    breakdown: dict | None = None     # itemised parts as the dealer wrote them (see sealed.core.breakdown)


@dataclass(frozen=True)
class Inbound:
    """One reply: an email from the dealer, or Fo's summary of a call."""
    dealer_id: str
    source: str                       # "email" | "call" | "fo_relay"
    text: str
    parsed: ParsedReply
    in_reply_to: str | None = None
    from_addr: str | None = None
    turns: tuple[dict, ...] = ()      # call transcript turns ({"role": "assistant"|"user", "text": ...})


@dataclass(frozen=True)
class Quote:
    quote_id: str
    dealer_id: str
    reply_id: str
    source: str
    price_total: float | None
    firm: bool
    meets_spec: bool
    meets_deadline: bool | None
    available: str | None
    hedge_words: tuple[str, ...]
    declined: bool
    suspicious: bool
    received_at: float
    breakdown: dict | None = None
    breakdown_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """A strategy decision. Never contains anything derived from the ceiling."""
    dealer_id: str
    action: str                       # counter | price_match | firm_up | propose | needs_human
    kind: str                         # rfq | firm_up | price_match | counter | booking | cancel | none
    channel: str = "email"            # email | call
    round: int = 1
    offer: float | None = None        # only ever the target
    own_price: float | None = None
    own_quote_id: str | None = None
    cited_quote_id: str | None = None
    cited_price: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class Outbound:
    message_id: str
    dealer_id: str
    kind: str
    channel: str
    round: int
    to: str
    subject: str | None
    body: str                         # exact email body, or the call script
    fo_instruction: str
    offer: float | None = None
    cited_quote_id: str | None = None
    allowed_prices: frozenset[float] = field(default_factory=frozenset)
    booking_approved: bool = False
    # (value, source, quote_id): source is "ledger" or "target"; every price the message may state
    price_sources: tuple[tuple[float, str, str | None], ...] = ()
