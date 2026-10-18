"""Decode bounded Gmail messages and associate them with known outreach.

No Fo, parser, vault or strategy imports belong here. Authentication-Results is
accepted only from the first such header delivered by the authenticated Gmail
connection; ARC and later headers supplied by a sender cannot establish trust.
"""

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser

from email_reply_parser import EmailReplyParser


def addresses(values: list[str]) -> tuple[str, ...]:
    return tuple(address.strip().lower() for _, address in getaddresses(values) if address)


def message_ids(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"<[^<>\s]+>", value))


class _HTMLText(HTMLParser):
    """Text only; never load remote assets or include scripts/quoted HTML."""

    def __init__(self, *, strip_quotes=True):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.strip_quotes = strip_quotes

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        quoted = (tag in {"script", "style", "head"} or (self.strip_quotes and (
                  tag == "blockquote" or "gmail_quote" in (attributes.get("class") or "").split()
                  or (attributes.get("id") or "").lower() == "divrplyfwdmsg")))
        hidden = quoted or any(hidden for _, hidden in self.stack)
        if tag not in {"br", "hr", "img", "meta", "link", "input", "wbr"}:
            self.stack.append((tag, hidden))
        if not hidden and tag in {"br", "p", "div", "li", "tr", "hr"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if not any(hidden for _, hidden in self.stack) and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not any(hidden for _, hidden in self.stack):
            self.parts.append(data)


def body_text(message: EmailMessage) -> tuple[str, str]:
    # get_body ignores attachments and handles multipart/alternative/related.
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", ""
    raw = part.get_content()
    if not isinstance(raw, str):
        return "", ""
    if part.get_content_type() == "text/html":
        evidence = _HTMLText(strip_quotes=False)
        evidence.feed(raw)
        parser = _HTMLText()
        parser.feed(raw)
        reply = "".join(parser.parts)
        raw = "".join(evidence.parts)
    else:
        reply = raw
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Also remove unprefixed Outlook history. Never let our original request's
    # numbers become the dealer's own quote. The raw evidence is retained.
    raw_reply = re.split(r"(?im)^\s*(?:-{2,}\s*Original Message\s*-{2,}|"
                         r"From:[^\n]+\n(?:Sent:|Date:))", reply.replace("\r\n", "\n"), maxsplit=1)[0]
    clean = EmailReplyParser.parse_reply(raw_reply).strip()
    return raw, clean


@dataclass(frozen=True)
class MailMessage:
    gmail_id: str
    uid: str
    thread_id: str
    received_at: str                  # Gmail INTERNALDATE, never the sender's Date
    message_id: str
    reply_to_ids: tuple[str, ...]
    references: tuple[str, ...]
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    raw_text: str
    clean_text: str
    spf: str
    dkim: str
    dmarc: str
    authenticated: bool
    flags: tuple[str, ...] = ()

    @property
    def evidence_ref(self) -> str:
        return f"gmail:{self.gmail_id}"


def decode_message(raw: bytes, *, gmail_id: str, uid: str, thread_id: str,
                   received_at: str) -> MailMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    senders = addresses([str(v) for v in message.get_all("From", [])])
    flags = []
    if len(senders) != 1 or len(message.get_all("From", [])) != 1:
        flags.append("invalid_sender")
    sender = senders[0] if len(senders) == 1 else ""
    ids = message_ids(str(message.get("Message-ID", "")))
    if len(ids) != 1 or len(message.get_all("Message-ID", [])) != 1:
        flags.append("invalid_message_id")
    if len(message.get_all("Subject", [])) > 1:
        flags.append("duplicate_subject")
    auth_headers = message.get_all("Authentication-Results", [])
    auth = str(auth_headers[0]) if auth_headers else ""
    trusted = bool(re.match(r"^\s*mx\.google\.com\s*;", auth, re.I))
    results = {}
    for method in ("spf", "dkim", "dmarc"):
        result = re.search(rf"(?:^|;)\s*{method}=(\w+)", auth, re.I) if trusted else None
        results[method] = result.group(1).lower() if result else "unknown"
    dmarc = re.search(r"(?:^|;)\s*dmarc=pass\b([^;]*)", auth, re.I) if trusted else None
    domain = re.search(r"\bheader\.from=([a-z0-9.-]+)", dmarc.group(1), re.I) if dmarc else None
    authenticated = bool(sender and domain and domain.group(1).lower() == sender.rsplit("@", 1)[-1])
    if not authenticated:
        flags.append("sender_auth_unverified")
    try:
        raw_text, clean = body_text(message)
    except (LookupError, ValueError, UnicodeError, RecursionError):
        raw_text, clean = "", ""
        flags.append("body_decode_failed")
    if not clean:
        flags.append("empty_reply")
    if any(part.defects for part in message.walk()):
        flags.append("malformed_mime")
    reply_ids = message_ids(" ".join(str(v) for v in message.get_all("In-Reply-To", [])))
    if len(set(reply_ids)) > 1:
        flags.append("ambiguous_reply_header")
    refs = " ".join(str(v) for v in message.get_all("References", []))
    return MailMessage(gmail_id, uid, thread_id, received_at, ids[0] if len(ids) == 1 else "",
                       reply_ids, message_ids(refs), sender,
                       addresses([str(v) for v in message.get_all("To", [])]),
                       addresses([str(v) for v in message.get_all("Cc", [])]),
                       str(message.get("Subject", "")), raw_text, clean,
                       results["spf"], results["dkim"], results["dmarc"], authenticated, tuple(flags))


@dataclass(frozen=True)
class Binding:
    """Trusted outreach metadata, not values extracted from dealer text.

    One binding per sent email. Phone-only contacts require an independently
    reviewed email association before a binding can be created.
    """
    job_id: str
    run_id: str
    dealer_id: str
    sender: str
    tag: str                         # full bracketed subject tag
    sent_at: str                     # timezone-aware ISO timestamp
    outbound_id: str
    outbound_message_id: str = ""    # actual RFC Message-ID of the sent copy
    thread_id: str = ""

    def __post_init__(self):
        if (not all((self.job_id, self.run_id, self.dealer_id, self.outbound_id))
                or addresses([self.sender]) != (self.sender.lower(),)
                or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", self.sender)
                or not re.fullmatch(r"\[SEALED-[A-Za-z0-9_-]+\]", self.tag)
                or datetime.fromisoformat(self.sent_at).tzinfo is None
                or (self.outbound_message_id and message_ids(self.outbound_message_id) != (self.outbound_message_id,))):
            raise ValueError("Invalid outreach binding; use a known sender, tag and timezone-aware sent time.")

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.job_id, self.run_id, self.dealer_id


@dataclass(frozen=True)
class Match:
    status: str                      # ready | review | ignored
    reason: str
    binding: Binding | None = None
    in_reply_to: str | None = None   # our outbound ID, only from a real RFC link


def match_message(message: MailMessage, bindings: list[Binding], mailbox: str) -> Match:
    eligible = [b for b in bindings if b.sender.lower() == message.from_addr
                and datetime.fromisoformat(message.received_at) >= datetime.fromisoformat(b.sent_at)]
    if not eligible:
        return Match("ignored", "unknown_sender_or_before_outreach")
    tagged = [b for b in eligible if b.tag in message.subject]
    linked = [b for b in eligible if b.outbound_message_id and b.outbound_message_id in message.reply_to_ids + message.references]
    threaded = [b for b in eligible if b.thread_id and b.thread_id == message.thread_id]
    # References often contain all previous sends in a conversation. Their order
    # is chronological; prefer the most recent referenced outbound for linkage.
    candidates = tagged or linked or threaded or eligible
    scopes = {b.scope for b in candidates}
    if len(scopes) != 1:
        return Match("review", "ambiguous_job")
    scope = next(iter(scopes))
    if any(b.scope != scope for b in linked + threaded):
        return Match("review", "conflicting_thread")
    known_tags = set(re.findall(r"\[SEALED-[A-Za-z0-9_-]+\]", message.subject))
    if known_tags and not tagged:
        return Match("review", "unknown_subject_tag")
    if known_tags - {b.tag for b in tagged}:
        return Match("review", "conflicting_subject_tags")
    same_scope = [b for b in eligible if b.scope == scope]
    ref_order = {ref: i for i, ref in enumerate(message.references + message.reply_to_ids)}
    linked_here = [b for b in same_scope if b.outbound_message_id in ref_order]
    binding = max(linked_here, key=lambda b: ref_order[b.outbound_message_id]) if linked_here else max(
        candidates, key=lambda b: datetime.fromisoformat(b.sent_at))
    if mailbox.lower() not in message.to + message.cc:
        return Match("review", "mailbox_not_addressed", binding)
    if message.flags or not message.authenticated:
        return Match("review", "message_checks_failed", binding)
    if not (tagged or linked or threaded):
        return Match("review", "sender_time_fallback", binding)
    return Match("ready", "tag" if tagged else "references" if linked else "gmail_thread", binding,
                 binding.outbound_id if linked_here else None)


def staged_record(message: MailMessage, match: Match) -> dict:
    return {"message": asdict(message), "match": asdict(match), "evidence_ref": message.evidence_ref}
