"""Conservative checks for Fo-composed email; never execute or trust draft instructions.

This is a bounded rule checker, not a proof of arbitrary prose. Unsupported formats,
ambiguous price roles and known ungrounded claims require review, not automatic yes.
"""

import re
from math import isfinite
from urllib.parse import urlsplit

from sealed.core.models import EmailDraft, Outbound
from sealed.core.numbers import numbers_in

DRAFT_PATTERN = re.compile(
    r"\A\s*(?:Here's the draft:\s*)?<<<EMAIL_DRAFT\r?\n"
    r"To: (?P<to>[^\r\n]+)\r?\nCC: ?(?P<cc>[^\r\n]*)\r?\n"
    r"Subject: (?P<subject>[^\r\n]+)\r?\nBody:\r?\n(?P<body>.*?)\r?\nEMAIL_DRAFT(?:>>>)?"
    r"\s*(?:Good to send\?|Ready to send\?)?\s*\Z", re.S)
NUMERIC = re.compile(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s?(k|thousand|grand)?\b", re.I)
CURRENCY_AMOUNT = re.compile(r"(?:\$|\b(?:USD|CAD|EUR|GBP))\s*(\d[\d,]*(?:\.\d+)?)|"
                             r"(\d[\d,]*(?:\.\d+)?)\s*(?:USD|CAD|EUR|GBP|dollars)\b", re.I)
COMPETING = re.compile(r"\b(other|another|competing|competitor|lowest|best competing)\b", re.I)
EXTRA_ACTION = re.compile(r"\b(schedule|remind\w*|follow[- ]?up|callback|call you|visit|test drive|"
                          r"charge|card on file|bank details|wire transfer|send (?:another|a second)|"
                          r"contact (?:another|a second)|email (?:another|a second))\b", re.I)
PROMISE = re.compile(r"\b(guarantee\w*|free (?:delivery|shipping|service)|in stock|available (?:now|today|tomorrow)|"
                     r"deliver(?:y|ed)? (?:by|on|tomorrow)|discount\w*|financ\w*|per month|monthly|"
                     r"installment\w*|plus fees|excluding (?:fees|taxes))\b", re.I)
COMMITMENT = re.compile(r"\b(accept\w*|agree\w*|confirm\w*|order\w*|pay|paid|payment\w*|deposit\w*|"
                        r"purchas\w*|sign\w*|commit\w*|proceed\w*|reserve\w*|hold\s+the\s+car|book\w*)\b", re.I)


def fo_signature_link(value: str, *, images: bool = False) -> bool:
    """Known Fo signature destinations/assets, verified from the actual test copy.

    Normalize www/trailing slash, never arbitrary paths, queries or credentials.
    """
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    try:
        url = urlsplit(value)
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment or url.port:
            return False
        host = (url.hostname or "").lower().removeprefix("www.")
        path = url.path.rstrip("/")
        if (host, path) in {("wajo.ai", "/Fo"), ("instagram.com", "/wajo_ai"),
                            ("x.com", "/wajo_ai"), ("linkedin.com", "/company/wajo-ai")}:
            return True
        return images and host == "media.wajo.ai" and path in {
            "/uploads/wajo-logo-light-64-de1291cd.png", "/uploads/social-instagram-2x-dbfff657.png",
            "/uploads/social-x-2x-ef367141.png", "/uploads/social-linkedin-2x-e9a4aead.png"}
    except ValueError:
        return False


def parse_draft(preview: str) -> EmailDraft | None:
    # The live Fo response omits the closing >>> even when explicitly requested.
    # Accept that observed whole-line terminator, keeping the complete envelope
    # anchored and rejecting nested markers and text outside the draft.
    found = DRAFT_PATTERN.fullmatch(preview)
    if not found:
        return None
    draft = EmailDraft(**found.groupdict())
    if (any(marker in draft.body for marker in ("<<<", ">>>"))
            or any(line.strip() == "EMAIL_DRAFT" for line in draft.body.splitlines())):
        return None
    return draft


def format_draft(draft: EmailDraft) -> str:
    return (f"<<<EMAIL_DRAFT\nTo: {draft.to}\nCC: {draft.cc}\nSubject: {draft.subject}\n"
            f"Body:\n{draft.body}\nEMAIL_DRAFT>>>\nGood to send?")


def mask_known_prices(text: str, allowed: frozenset[float]) -> str:
    """Mask public allowed numeric prices in any digit format before the secret checker.

    English price wording is not accepted by the draft contract (see numbers rule).
    """
    def replace(match):
        values = numbers_in(match.group(0))
        return "[allowed price]" if values and values[0] in allowed else match.group(0)
    return NUMERIC.sub(replace, text)


def brief_authorized(msg: Outbound) -> bool:
    brief = msg.email_brief
    if brief is None or msg.channel != "email":
        return False
    prices = {p for p in (brief.own_price, brief.competing_price, brief.offer, brief.approved_price) if p is not None}
    if any(not isfinite(p) or p <= 0 for p in prices) or msg.allowed_prices != prices or msg.offer != brief.offer:
        return False
    if {p for p, _, _ in msg.price_sources} != prices:
        return False
    if msg.booking_approved != (msg.kind == "booking" and brief.approved_price is not None):
        return False
    if brief.offer is not None and (msg.kind != "counter" or (brief.offer, "target", None) not in msg.price_sources):
        return False
    if brief.competing_price is not None and (msg.kind != "price_match" or not msg.cited_quote_id
            or (brief.competing_price, "ledger", msg.cited_quote_id) not in msg.price_sources):
        return False
    if brief.approved_price is not None and msg.kind != "booking":
        return False
    return (msg.kind in {"rfq", "firm_up", "cancel"}
            or (msg.kind == "counter" and brief.offer is not None)
            or (msg.kind == "price_match" and brief.competing_price is not None)
            or (msg.kind == "booking" and brief.approved_price is not None))


def payment_text(text: str, *, booking: bool) -> str:
    text = text.replace("No payment is being made by email.", "")
    if booking:
        text = re.sub(r"\bpurchase paperwork\b", "paperwork", text, flags=re.I)
    return text


def review_rules(msg: Outbound, draft: EmailDraft) -> list[dict]:
    brief = msg.email_brief
    if brief is None:
        raise ValueError("A composed email requires a brief.")
    body = draft.body
    normalized = re.sub(r"\s+", " ", body).strip().lower()
    # Routing is explicit and exact; added CC/BCC/Reply-To or instructions in the
    # body are never silently accepted as envelope metadata.
    routing = (draft.to == msg.to and draft.cc == msg.cc and draft.subject == msg.subject
               and not any("\r" in h or "\n" in h for h in (draft.to, draft.cc, draft.subject))
               and not re.search(r"(?im)^\s*(?:to|cc|bcc|reply-to|subject):", body))
    source_numbers = {1} | set(numbers_in(" ".join((brief.spec, brief.zip, brief.timeline))))
    actual_numbers = set(numbers_in(body))
    explicit_money = {float((m.group(1) or m.group(2)).replace(",", "")) for m in CURRENCY_AMOUNT.finditer(body)}
    # Do not approve English-written price amounts: a single canonical digit
    # representation also keeps public-price masking independent of the ceiling.
    word_prices = set(numbers_in(NUMERIC.sub(" ", body))) & msg.allowed_prices
    written_money = re.search(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                              r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
                              r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|grand)"
                              r"\s+(?:dollars?|USD|CAD|EUR|GBP)\b", body, re.I)
    currencies = {value.upper() for value in re.findall(r"\b(?:USD|CAD|EUR|GBP)\b", body, re.I)}
    numbers_ok = (not (actual_numbers - source_numbers - msg.allowed_prices)
                  and explicit_money <= msg.allowed_prices and not word_prices and not written_money
                  and currencies <= {brief.currency}
                  and ("$" not in body or brief.currency in {"USD", "CAD"})
                  and "€" not in body and "£" not in body and "%" not in body
                  and not re.search(r"\$\s*-|-\s*\$|\b(?:minus|negative)\b", body, re.I))
    # Only simple, unambiguous price attribution is eligible for automatic yes.
    # Do not let an allowed own quote become an invented competing quote or offer.
    roles = set()
    roles_ok = True
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
        values = set(numbers_in(sentence)) & msg.allowed_prices
        if not values:
            # A known model year/ZIP cannot be repurposed as an unmarked price.
            if numbers_in(sentence) and re.search(
                    r"\b(?:could|can|would)\b.*\b(?:do|meet|offer)\b|"
                    r"\byour (?:written )?(?:total|quote) (?:is|was|at)\b", sentence, re.I):
                roles_ok = False
            continue
        if COMPETING.search(sentence):
            expected, role = brief.competing_price, "competing"
        elif re.search(r"\b(could|can|would|able)\b.*\b(do|meet|offer|work|total)\b|\bwould\b.*\bwork\b", sentence, re.I):
            expected, role = brief.offer, "offer"
        elif re.search(r"\b(paperwork|proceed)\b", sentence, re.I):
            expected, role = brief.approved_price, "approved"
        elif re.search(r"\byour\b|\byou (?:quoted|wrote)\b|\b(?:written )?total you (?:gave|provided)\b", sentence, re.I):
            expected, role = brief.own_price, "own"
        else:
            expected, role = None, "unknown"
        roles_ok &= expected is not None and values == {expected}
        roles.add(role)
    required_role = {"counter": "offer", "price_match": "competing", "booking": "approved"}.get(msg.kind)
    roles_ok &= required_role is None or required_role in roles
    no_fake_leverage = brief.competing_price is not None or msg.kind == "cancel" or not COMPETING.search(body)
    # Ordinary assistant signatures may include Fo's known links; other URLs and
    # email addresses would introduce a new contact/action outside this brief.
    email_addrs = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", body)
    urls = re.findall(r"(?:https?://|www\.)[^\s<>]+|\b(?:instagram\.com|x\.com|linkedin\.com)/[^\s<>]+", body, re.I)
    links_ok = all(fo_signature_link(url.rstrip(".,;·")) for url in urls)
    contacts_ok = all(address in {msg.to, msg.cc} for address in email_addrs)
    safe_text = payment_text(body, booking=msg.booking_approved)
    if msg.booking_approved:
        safe_text = re.sub(r"\b(?:would like to |like to )?proceed\b", "", safe_text, flags=re.I)
    # A request to confirm availability is not a claim that the buyer confirmed a deal.
    safe_text = re.sub(r"\b(?:could you|please) confirm (?:availability|the total|quote validity)\b", "", safe_text, flags=re.I)
    # A conditional request for the seller's written total is not buyer approval.
    # Keep the exception inside that question; affirmative confirmation remains blocked.
    safe_text = re.sub(
        r"(\b(?:could|can|would) you (?:do|meet|offer)\b[^.!?\n]*?)"
        r"\bconfirmed as a written out[- ]the[- ]door total\b",
        r"\1stated as a written out-the-door total", safe_text, flags=re.I)
    identity = bool(re.search(r"\b(?:AI|artificial intelligence) assistant\b", body, re.I) and re.search(r"\bFo\b", body))
    spec_ok = msg.kind == "cancel" or re.sub(r"\s+", " ", brief.spec).lower() in normalized
    intent = {
        "rfq": bool(re.search(r"out[- ](?:the[- ])?door", body, re.I) and re.search(r"\bwritten\b", body, re.I)
                    and all(re.search(rf"\b{term}\b", body, re.I) for term in ("fees", "taxes"))
                    and re.search(r"availab", body, re.I) and re.search(r"valid", body, re.I)),
        "firm_up": bool(re.search(r"out[- ](?:the[- ])?door", body, re.I) and re.search(r"\bwritten\b", body, re.I)
                        and all(re.search(rf"\b{term}\b", body, re.I) for term in ("fees", "taxes"))),
        "price_match": "competing" in roles and bool(re.search(r"revis|improv|match", body, re.I)),
        "counter": "offer" in roles and bool(re.search(r"out[- ](?:the[- ])?door", body, re.I)),
        "booking": "approved" in roles and bool(re.search(r"paperwork", body, re.I))
                   and bool(re.search(r"out[- ](?:the[- ])?door", body, re.I)),
        "cancel": bool(re.search(r"another dealer|different dealer|declin|chosen elsewhere", body, re.I)),
    }.get(msg.kind, False)
    checks = {
        "draft_routing": routing,
        "brief_numbers": numbers_ok,
        "price_roles": roles_ok,
        "facts_grounded": bool(no_fake_leverage and not PROMISE.search(body)),
        "commitment_words": not COMMITMENT.search(safe_text),
        "action_scope": not EXTRA_ACTION.search(body) and links_ok and contacts_ok,
        "safety_line": identity,
        "brief_intent": bool(spec_ok and intent and re.search(r"reply[- ]?all", body, re.I)),
    }
    return [{"rule": name, "passed": bool(passed)} for name, passed in checks.items()]
