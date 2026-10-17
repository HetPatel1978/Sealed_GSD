"""Message builder: fixed templates in Fo's voice. Every email has two parts: the instruction we give Fo
(`fo_instruction`) and the exact text the dealer reads (`email_body`). Calls get a short script.

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

# Exact phrases the guard exempts from the commitment-word rule (they say we are NOT committing).
SAFETY_PHRASES = (
    "This is a request for a quote only, and nothing is being agreed or paid.",
    "I can't agree to anything on this call.",
    "No payment is being made by email.",
)


def money(x: float) -> str:
    return f"${x:,.0f}"


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


def wrap_call_for_fo(dealer_name: str, phone: str, script: str) -> str:
    return (
        f"Please place one phone call to {dealer_name} at {phone}.\n\n"
        "When someone answers, say only the following, then end the call politely:\n"
        f"\"{script}\"\n\n"
        "Do not add any other claims or details. Do not agree to anything, do not make any commitment, "
        "purchase or payment, and do not state any price other than the one in the script. If you're asked a "
        "question you can't answer from this script, say you'll pass it on and end the call. "
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
                f"to {job.buyer}. " + SAFETY_PHRASES[2])
    elif d.kind == "cancel":
        core = (f"{DISCLOSURE} writing on behalf of {job.buyer}. Thank you for your quote. {job.buyer} has decided "
                "to go with another dealer. We appreciate your time.")
    else:
        raise ValueError(f"no email template for kind {d.kind!r}")
    return hello + core + _sign(job)


def call_script(job: Job, d: Decision) -> str:
    if d.kind == "rfq":
        return (f"Hi, {DISCLOSURE} calling on behalf of a buyer about a new {job.details}. I emailed your internet "
                "sales team asking for an out-the-door total. Could you please reply to that email with the total? "
                + SAFETY_PHRASES[1] + " Thank you.")
    if d.kind == "price_match":
        return (f"Hi, {DISCLOSURE} calling on behalf of a buyer about the {job.details}. The lowest written "
                f"out-the-door total the buyer has so far for the same car is {money(d.cited_price)}. If you'd like "
                "to revise your quote, please send it by email. " + SAFETY_PHRASES[1] + " Thank you.")
    raise ValueError(f"no call script for kind {d.kind!r}")


def subject_for(job: Job, tag: str, kind: str) -> str:
    base = f"Out-the-door quote request: {job.item} [{tag}]"
    return base if kind == "rfq" else f"Re: {base}"


def build(job: Job, d: Decision, message_id: str, tag: str, cc: str, approved_price: float | None = None) -> Outbound:
    dealer = job.dealer(d.dealer_id)
    allowed = {p for p in (d.own_price, d.cited_price, d.offer, approved_price) if p is not None}
    if d.channel == "call":
        body = call_script(job, d)
        return Outbound(message_id, d.dealer_id, d.kind, "call", d.round, dealer.phone or "", None, body,
                        wrap_call_for_fo(dealer.name, dealer.phone or "", body), d.offer, d.cited_quote_id,
                        frozenset(allowed))
    body = email_body(job, d, dealer.name, approved_price)
    subject = subject_for(job, tag, d.kind)
    return Outbound(message_id, d.dealer_id, d.kind, "email", d.round, dealer.email, subject, body,
                    wrap_for_fo(dealer.email, cc, subject, body), d.offer, d.cited_quote_id, frozenset(allowed),
                    booking_approved=d.kind == "booking" and approved_price is not None)


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
