"""Message builder: fixed templates in Fo's voice. Every email has two parts: the instruction we give Fo
(`fo_instruction`) and the exact text the dealer reads (`email_body`). Calls get a sealed brief: a goal,
the only facts Fo may use, and fixed hard limits; Fo runs the conversation in its own words.

No LLM. Inputs are the job, the decision and ledger values only; there is no ceiling parameter.
Fo refuses to send text written as the user (spike 1b), so everything says "I'm Fo, an AI assistant".
"""

import secrets
from dataclasses import dataclass

from sealed.core.models import Decision, Job, Outbound

TAG_PREFIX = "SEALED-"
BODY_START = "<<<EMAIL_BODY"
BODY_END = "EMAIL_BODY>>>"
DISCLOSURE = "I'm Fo, an AI assistant"

# Fixed hard limits for every call brief.
CALL_LIMITS = (
    "Hard limits:\n"
    "- Say at the start that you are Fo, an AI assistant calling on behalf of the buyer.\n"
    "- Do not agree to, reserve, hold, order or pay for anything, and do not give or ask for payment details.\n"
    "- Do not say any number that is not in the allowed facts.\n"
    "- If asked how much the buyer can spend, or about a deposit or holding the car, say only: "
    "\"The buyer is comparing written out-the-door totals.\"\n"
    "- If the dealer says a price, do not accept or negotiate it; ask them to put it in an email.\n"
    "- Do not promise any follow-up, visit, test drive or callback.\n"
    "- Keep the call short and polite."
)
NO_PAYMENT_BY_EMAIL = "No payment is being made by email."

# Exact text the guard exempts from the commitment and limit-language rules (it says we are NOT committing).
SAFETY_PHRASES = (
    "This is a request for a quote only, and nothing is being agreed or paid.",
    NO_PAYMENT_BY_EMAIL,
    CALL_LIMITS,
)


def money(x: float) -> str:
    return f"${x:,.2f}".removesuffix(".00")


def new_tag() -> str:
    """8-character id, e.g. SEALED-3F9A1C07."""
    return TAG_PREFIX + secrets.token_hex(4).upper()


def wrap_for_fo(to: str, cc: str, subject: str, email_body: str) -> str:
    return (
        "Please send the email below on my behalf.\n\n"
        f"To: {to}\n"
        f"CC: {cc}\n"
        f"Subject (use EXACTLY as written, character for character): {subject}\n\n"
        "Body: send the text between the markers VERBATIM. Do not add, remove, rephrase or reformat "
        "anything. Do not add a greeting, signature, links, or any other content. The body already asks "
        "the recipient to reply-all.\n"
        f"{BODY_START}\n{email_body}\n{BODY_END}\n\n"
        "This is a request for information only. Do not agree to, accept, order, or pay for anything, "
        "and do not reply to the recipient on my behalf."
    )


def wrap_call_for_fo(dealer_name: str, phone: str, brief: str) -> str:
    return (
        f"Please place one phone call to {dealer_name} at {phone}.\n\n"
        "Run the conversation in your own words, using only the brief between the markers. Stay inside its "
        "allowed facts and hard limits.\n"
        f"<<<CALL_BRIEF\n{brief}\nCALL_BRIEF>>>\n\n"
        "After the call, give me a short summary, including any price the dealer said, word for word."
    )


def _sign(job: Job) -> str:
    return f"\n\nPlease use reply-all so {job.buyer} sees your answer.\n\nThank you,\nFo, AI assistant to {job.buyer}"


def email_body(job: Job, d: Decision, dealer_name: str, approved_price: float | None = None) -> str:
    hello = f"Hello {dealer_name} team,\n\n"
    car = job.details
    if d.kind == "rfq":
        core = (f"{DISCLOSURE} writing on behalf of {job.buyer}, who plans to buy one new {car} {job.timeline}, "
                f"near ZIP {job.zip}.\n\nCould you reply with your written out-the-door total for this exact car, "
                "including all fees, taxes and any dealer add-ons, and how long the price is valid? "
                + SAFETY_PHRASES[0])
    elif d.kind == "firm_up":
        core = (f"{DISCLOSURE} writing on behalf of {job.buyer}. Thanks for the reply. {job.buyer} is comparing "
                f"out-the-door totals only. Could you send the out-the-door total for the {car}, including all "
                "fees, taxes and add-ons?")
    elif d.kind == "price_match":
        core = (f"{DISCLOSURE} writing on behalf of {job.buyer}. Your out-the-door total is {money(d.own_price)}. "
                f"The lowest written out-the-door total {job.buyer} has received for the same car is "
                f"{money(d.cited_price)}. Would you like to revise your quote? If so, please reply in writing.")
    elif d.kind == "counter":
        core = (f"{DISCLOSURE} writing on behalf of {job.buyer}. Thanks for your written total of "
                f"{money(d.own_price)}. Could you do {money(d.offer)} out-the-door for the same car?")
    elif d.kind == "booking":
        core = (f"{DISCLOSURE} writing on behalf of {job.buyer}. {job.buyer} would like to proceed with your written "
                f"out-the-door total of {money(approved_price)} for the {car}. Please send the purchase paperwork "
                f"to {job.buyer}. " + NO_PAYMENT_BY_EMAIL)
    elif d.kind == "cancel":
        core = (f"{DISCLOSURE} writing on behalf of {job.buyer}. Thank you for your quote. {job.buyer} has decided "
                "to go with another dealer. We appreciate your time.")
    else:
        raise ValueError(f"no email template for kind {d.kind!r}")
    return hello + core + _sign(job)


def call_brief(job: Job, d: Decision, dealer_name: str, reply_to: str, emailed: bool = True) -> str:
    """A sealed brief: goal, the only facts Fo may use, and fixed hard limits. Fo talks in its own words.

    `emailed` is False for a dealer we have no email address for (phone-only): the brief must not claim we emailed.
    """
    where = f"reply to the email we sent, or email {reply_to}" if emailed else f"email {reply_to}"
    facts = [
        f"- Introduce yourself: \"{DISCLOSURE} calling on behalf of {job.buyer}.\"",
        f"- The car: {job.details}",
        f"- The buyer's first name: {job.buyer}",
        f"- The buyer's ZIP: {job.zip}",
        f"- Timeline: {job.buyer} plans to buy {job.timeline}",
        f"- Where to send the quote: {where}",
    ]
    if d.kind == "rfq":
        goal = (f"Get {dealer_name} to email a written out-the-door total for this exact car, including all fees, "
                "taxes and add-ons.")
        facts.append("- We emailed their internet sales team asking for an out-the-door total and have no reply yet."
                     if emailed else "- This call is our first contact with them.")
    elif d.kind == "price_match":
        goal = (f"Tell {dealer_name} the lowest written out-the-door total so far, and ask them to email a revised "
                "written total if they want to revise.")
        facts.append(f"- The lowest written out-the-door total {job.buyer} has received for the same car: "
                     f"{money(d.cited_price)}")
    else:
        raise ValueError(f"no call brief for kind {d.kind!r}")
    return f"Goal: {goal}\n\nAllowed facts (the only things you may say):\n" + "\n".join(facts) + "\n\n" + CALL_LIMITS


def subject_for(job: Job, tag: str, kind: str) -> str:
    base = f"Out-the-door quote request: {job.item} [{tag}]"
    return base if kind == "rfq" else f"Re: {base}"


def price_sources(d: Decision, approved_price: float | None) -> tuple[tuple[float, str, str | None], ...]:
    out = []
    if d.own_price is not None:
        out.append((d.own_price, "ledger", d.own_quote_id))
    if d.cited_price is not None:
        out.append((d.cited_price, "ledger", d.cited_quote_id))
    if d.offer is not None:
        out.append((d.offer, "target", None))
    if approved_price is not None:
        out.append((approved_price, "ledger", d.own_quote_id))
    return tuple(out)


def number_provenance(msg: Outbound, job: Job) -> list[dict]:
    """Every distinct number in the message and where it came from: a ledger quote, the target, or the job spec
    (model year, ZIP, timeline). Anything else is reported as `unknown` (the guard blocks unknown prices)."""
    from sealed.core.guard import MONEY, money_values
    from sealed.core.numbers import numbers_in
    out, seen = [], set()
    for v in money_values(msg.body):
        if v in seen:
            continue
        seen.add(v)
        src = next((s for s in msg.price_sources if s[0] == v), None)
        item = {"value": v, "source": src[1] if src else "unknown"}
        if src and src[2]:
            item["quote_id"] = src[2]
        out.append(item)
    job_numbers = set(numbers_in(" ".join((job.item, job.details, job.zip, job.timeline, "quantity 1"))))
    for v in numbers_in(MONEY.sub(" ", msg.body)):
        if v not in seen:
            seen.add(v)
            out.append({"value": v, "source": "job" if v in job_numbers else "unknown"})
    return out


def build(job: Job, d: Decision, message_id: str, tag: str, cc: str, approved_price: float | None = None,
          email_to: str | None = None) -> Outbound:
    """`email_to` overrides the dealer's email with a phone-only address attributed by trusted ingestion."""
    dealer = job.dealer(d.dealer_id)
    to = email_to or dealer.email
    allowed = {p for p in (d.own_price, d.cited_price, d.offer, approved_price) if p is not None}
    sources = price_sources(d, approved_price)
    if d.channel == "call":
        body = call_brief(job, d, dealer.name, cc, emailed=bool(to))
        return Outbound(message_id, d.dealer_id, d.kind, "call", d.round, dealer.phone or "", None, body,
                        wrap_call_for_fo(dealer.name, dealer.phone or "", body), d.offer, d.cited_quote_id,
                        frozenset(allowed), price_sources=sources, dealer_name=dealer.name,
                        job_id=job.id, run_id=job.run_id)
    if not to:
        raise ValueError(f"no email address for dealer {dealer.id}")
    body = email_body(job, d, dealer.name, approved_price)
    subject = subject_for(job, tag, d.kind)
    return Outbound(message_id, d.dealer_id, d.kind, "email", d.round, to, subject, body,
                    wrap_for_fo(to, cc, subject, body), d.offer, d.cited_quote_id, frozenset(allowed),
                    booking_approved=d.kind == "booking" and approved_price is not None, price_sources=sources,
                    cc=cc, dealer_name=dealer.name, job_id=job.id, run_id=job.run_id)


# --- Legacy (spike 1b only): first-person text signed by the user. Fo refuses to send this. -------------

@dataclass(frozen=True)
class OutboundEmail:
    tag: str
    to: str
    cc: str
    subject: str
    email_body: str
    fo_instruction: str


def build_quote_request(to: str, cc: str, item: str, tag: str | None = None) -> OutboundEmail:
    tag = tag or new_tag()
    subject = f"Quote request: {item} [{tag}]"
    body = (
        "Hello,\n\n"
        f"I'm looking for a quote for the following: {item}.\n\n"
        "Could you please let me know:\n"
        "- The total price, including any fees and delivery\n"
        "- The delivery date you can commit to\n"
        "- How long this price is valid\n\n"
        "Please use reply-all when you answer so everyone on this thread sees it.\n\n"
        "Thank you,\n"
        "Het"
    )
    return OutboundEmail(tag, to, cc, subject, body, wrap_for_fo(to, cc, subject, body))
