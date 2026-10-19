"""Plain data types shared by the core. Note: Job has no ceiling field, on purpose (see sealed.private.vault)."""

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Dealer:
    """A dealer needs an email, a phone, or both. Phone-only dealers get the round-1 request as a call and are
    emailed later only at an address attributed to their call by trusted ingestion."""
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
    currency: str = "USD"


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
    contact_verified: bool = False    # trusted ingestion confirmed this sender belongs to a phone-only dealer;
                                      # never supplied by the quote parser
    mail_evidence: "MailEvidence | None" = None  # trusted Gmail metadata, never parser output
    subject: str | None = None


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
    verified: bool = False            # sender matches this job's configured dealer address
    numbers_grounded: bool = False   # code checked the total against the dealer's text
    mail_evidence: "MailEvidence | None" = None


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
class EmailBrief:
    """Public facts and price roles for one Fo-composed email. Never contains a ceiling."""
    buyer: str
    spec: str
    zip: str
    timeline: str
    currency: str = "USD"
    own_price: float | None = None
    competing_price: float | None = None
    offer: float | None = None
    approved_price: float | None = None


@dataclass(frozen=True)
class EmailDraft:
    to: str
    cc: str
    subject: str
    body: str


@dataclass(frozen=True)
class Outbound:
    message_id: str
    dealer_id: str
    kind: str
    channel: str
    round: int
    to: str
    subject: str | None
    body: str                         # email brief, legacy exact email, or call brief (see email_brief)
    fo_instruction: str
    offer: float | None = None
    cited_quote_id: str | None = None
    allowed_prices: frozenset[float] = field(default_factory=frozenset)
    booking_approved: bool = False
    # (value, source, quote_id): source is "ledger" or "target"; every price the message may state
    price_sources: tuple[tuple[float, str, str | None], ...] = ()
    cc: str = ""
    dealer_name: str = ""
    job_id: str = ""
    run_id: str = ""
    email_brief: EmailBrief | None = None  # None preserves legacy exact-send payloads/replays
    reply_to_message_id: str | None = None  # actual incoming RFC Message-ID, not an internal ID
    reply_thread_id: str | None = None      # trusted incoming Gmail thread
    call_buyer: str | None = None           # checked-plan call contract; None preserves legacy attempts


def outbound_fingerprint(msg: Outbound) -> str:
    """Stable payload identity; retains fingerprints of existing exact-send records."""
    parts = [msg.dealer_id, msg.kind, msg.channel, msg.round, msg.to, msg.subject, msg.body,
             msg.fo_instruction, msg.booking_approved]
    if msg.email_brief is not None:
        parts.append(asdict(msg.email_brief))
    if msg.reply_to_message_id is not None or msg.reply_thread_id is not None:
        parts.append([msg.reply_to_message_id, msg.reply_thread_id])
    if msg.call_buyer is not None:
        parts.append(["sealed-call-v1", msg.call_buyer])
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MailEvidence:
    gmail_id: str
    thread_id: str
    message_id: str
    received_at: str
    spf: str
    dkim: str
    dmarc: str
    authentication_source: str


@dataclass(frozen=True)
class EmailAudit:
    job_id: str
    run_id: str
    message_id: str
    fingerprint: str
    state: str                        # pending | passed | review
    reason_code: str | None
    basis: str                        # brief_constraints | exact_payload
    cc_copy_found: bool
    guard_passed: bool
    rules: tuple[dict, ...]
    candidate_refs: tuple[str, ...]
    evidence: MailEvidence | None = None

    @property
    def matched(self) -> bool:
        return self.state == "passed"

    @property
    def observation_id(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def validates(self, msg: Outbound) -> bool:
        return ((self.job_id, self.run_id, self.message_id, self.fingerprint)
                == (msg.job_id, msg.run_id, msg.message_id, outbound_fingerprint(msg)))

    @classmethod
    def from_dict(cls, data: dict) -> "EmailAudit":
        return cls(**{**data, "rules": tuple(data["rules"]), "candidate_refs": tuple(data["candidate_refs"]),
                      "evidence": MailEvidence(**data["evidence"]) if data.get("evidence") else None})
