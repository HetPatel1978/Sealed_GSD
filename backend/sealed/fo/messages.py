"""Build public email/call briefs; Fo composes the actual outreach in its own words.

Legacy exact-email helpers remain for recorded smoke sends and regression checks.

No LLM. Inputs are the job, the decision and ledger values only; there is no ceiling parameter.
Fo refuses to send text written as the user (spike 1b), so everything says "I'm Fo, an AI assistant".
"""

import secrets
from dataclasses import dataclass, replace

from sealed.core.models import Decision, EmailBrief, Job, Outbound
from sealed.core.call_policy import expected_plan

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
    "- Do not schedule or initiate retries, reminders, tasks or other outreach, even after voicemail or closed hours.\n"
    "- Use only the buyer identity in this brief; do not substitute the account owner or a remembered name.\n"
    "- Keep the call short and polite."
)
NO_PAYMENT_BY_EMAIL = "No payment is being made by email."

# Exact text the guard exempts from the commitment and limit-language rules (it says we are NOT committing).
SAFETY_PHRASES = (
    "This is a request for a quote only, and nothing is being agreed or paid.",
    NO_PAYMENT_BY_EMAIL,
    CALL_LIMITS,
)

EMAIL_LIMITS = (
    "Rules for this email:\n"
    "- Write as Fo, openly identifying yourself as an AI assistant acting for the buyer.\n"
    "- Use only the supplied facts and price roles. Do not invent quotes, availability, services or deadlines.\n"
    "- You may choose natural wording, greeting and a brief ordinary signature.\n"
    "- Do not discuss spending limits, invent leverage, or introduce any other monetary amount.\n"
    "- Do not agree to, reserve, order, sign, pay, or arrange deposits or payment details.\n"
    "- For an explicitly approved booking only, request purchase paperwork at the approved total.\n"
    "- Ask for a written reply and reply-all. Do not contact anyone else or schedule follow-ups.\n"
    "- Draft only first. Do not send until Sealed confirms this specific draft. Send it only once."
)

EMAIL_GOALS = {
    "rfq": "Ask for a written out-the-door total including all fees, taxes and add-ons, availability and quote validity.",
    "firm_up": "Ask for the missing written out-the-door total including all fees, taxes and add-ons, availability and quote validity.",
    "price_match": "State the verified lowest competing written total and ask for a revised written out-the-door total.",
    "counter": "Ask whether this dealer can meet the permitted offer as a written out-the-door total.",
    "booking": "Request purchase paperwork at the explicitly approved written total for this exact car. No payment is being made by email.",
    "cancel": "Politely decline this dealer's quote; the buyer chose another dealer. Make no further promises.",
}


def render_email_brief(kind: str, brief: EmailBrief) -> str:
    facts = [f"- Buyer's first name: {brief.buyer}", f"- Exact car: {brief.spec}",
             f"- Buyer's ZIP: {brief.zip}", f"- True timeline: {brief.timeline}",
             f"- Currency: {brief.currency}"]
    for label, value in (("Dealer's own verified written total", brief.own_price),
                         ("Lowest competing verified written total", brief.competing_price),
                         ("Only permitted offer", brief.offer),
                         ("User-approved written total for paperwork", brief.approved_price)):
        if value is not None:
            facts.append(f"- {label}: {money(value)} {brief.currency}")
    return (f"Goal: {EMAIL_GOALS[kind]}\n\nAllowed facts:\n" + "\n".join(facts)
            + f'\n\nIdentity: "{DISCLOSURE}" on behalf of {brief.buyer}.\n\n' + EMAIL_LIMITS)


def wrap_email_brief_for_fo(to: str, cc: str, subject: str, brief: str,
                           reply_to_message_id: str | None = None) -> str:
    threading = (f"Reply to the existing incoming email with Message-ID {reply_to_message_id}. "
                 "Use the existing email thread and set In-Reply-To to that Message-ID; do not create a new thread.\n\n"
                 if reply_to_message_id else "")
    return (
        "Compose one email in your own words using the brief below. This is a DRAFT request, not permission to send.\n\n"
        + threading +
        f"To: {to}\nCC: {cc}\nSubject (keep exactly): {subject}\n\n"
        f"<<<EMAIL_BRIEF\n{brief}\nEMAIL_BRIEF>>>\n\n"
        "Return the full draft in this exact envelope, followed by 'Good to send?':\n"
        "<<<EMAIL_DRAFT\nTo: [specified recipient]\nCC: [specified CC]\nSubject: [specified subject]\n"
        "Body:\n[your composed email]\nEMAIL_DRAFT>>>\n"
        "Keep the car specification verbatim. Keep monetary amounts as digits with the supplied currency. "
        "State each quote or offer in a separate sentence with its role clear. "
        "Use only this brief's facts, even when earlier conversation messages contain additional dates or terms. "
        "For a quote request, ask for a written reply with the total; avoid confirmation, agreement, order or "
        "payment language, including in disclaimers. If needed, use exactly: No payment is being made by email. "
        "Close the envelope with the literal line EMAIL_DRAFT>>> including all three greater-than characters. "
        "Do not send, schedule reminders, reply again, call, or take any other action without a separate instruction."
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


def call_plan_request(msg: Outbound) -> str:
    return ("Prepare a call plan ONLY. Do not dial, contact anyone or schedule anything yet. "
        "The following JSON is the sole authorized plan DATA; return it INLINE unchanged only "
        "if you can use this exact buyer identity and opening. Never substitute the account "
        "owner or a name from memory. If the platform prevents this, explain and do not dial. "
        "No emails, retries, reminders, tasks or follow-ups are authorized. Wait for a separate "
        "Sealed instruction before placing the one call.\n" + expected_plan(msg).model_dump_json())


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
          email_to: str | None = None, *, email_mode: str = "brief") -> Outbound:
    """`email_to` overrides the dealer's email with a phone-only address attributed by trusted ingestion."""
    dealer = job.dealer(d.dealer_id)
    to = email_to or dealer.email
    allowed = {p for p in (d.own_price, d.cited_price, d.offer, approved_price) if p is not None}
    sources = price_sources(d, approved_price)
    if d.channel == "call":
        body = call_brief(job, d, dealer.name, cc, emailed=bool(to))
        msg = Outbound(message_id, d.dealer_id, d.kind, "call", d.round, dealer.phone or "", None, body,
                        wrap_call_for_fo(dealer.name, dealer.phone or "", body), d.offer, d.cited_quote_id,
                        frozenset(allowed), price_sources=sources, dealer_name=dealer.name,
                        job_id=job.id, run_id=job.run_id, call_buyer=job.buyer)
        return replace(msg, fo_instruction=call_plan_request(msg))
    if not to:
        raise ValueError(f"no email address for dealer {dealer.id}")
    if email_mode not in {"brief", "exact"}:
        raise ValueError("Unknown email mode.")
    brief = EmailBrief(job.buyer, job.details, job.zip, job.timeline, job.currency,
                       d.own_price, d.cited_price, d.offer, approved_price) if email_mode == "brief" else None
    body = render_email_brief(d.kind, brief) if brief else email_body(job, d, dealer.name, approved_price)
    subject = subject_for(job, tag, d.kind)
    return Outbound(message_id, d.dealer_id, d.kind, "email", d.round, to, subject, body,
                    (wrap_email_brief_for_fo if brief else wrap_for_fo)(to, cc, subject, body),
                    d.offer, d.cited_quote_id, frozenset(allowed),
                    booking_approved=d.kind == "booking" and approved_price is not None, price_sources=sources,
                    cc=cc, dealer_name=dealer.name, job_id=job.id, run_id=job.run_id, email_brief=brief)


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
