"""A scripted Fo composer for offline simulation only; never passed to live Fo."""

from sealed.core.models import EmailDraft, Outbound
from sealed.fo.messages import DISCLOSURE, NO_PAYMENT_BY_EMAIL, money


def compose(msg: Outbound) -> EmailDraft:
    brief = msg.email_brief
    if brief is None:
        return EmailDraft(msg.to, msg.cc, msg.subject or "", msg.body)
    hello = f"Hello team,\n\n{DISCLOSURE} writing on behalf of {brief.buyer}. "
    def price(value):
        return money(value) if brief.currency in {"USD", "CAD"} else money(value).removeprefix("$")
    spec = f"The exact car is {brief.spec}. "
    if msg.kind in {"rfq", "firm_up"}:
        core = (f"{spec}The buyer plans to buy {brief.timeline}, near ZIP {brief.zip}. "
                "Could you send a written out-the-door total including all fees, taxes and add-ons? "
                "Please include availability and quote validity.")
    elif msg.kind == "price_match":
        core = (f"{spec}The lowest competing written out-the-door total is {price(brief.competing_price)} {brief.currency}. "
                "Would you like to email a revised written total?")
    elif msg.kind == "counter":
        own = f"Your written total is {price(brief.own_price)} {brief.currency}. " if brief.own_price is not None else ""
        core = f"{spec}{own}Could you do {price(brief.offer)} {brief.currency} out-the-door?"
    elif msg.kind == "booking":
        core = (f"{spec}Please send the purchase paperwork at the approved out-the-door total of {price(brief.approved_price)} {brief.currency}. "
                + NO_PAYMENT_BY_EMAIL)
    else:
        core = "Thank you for your quote. The buyer has chosen another dealer. Thank you for your time."
    return EmailDraft(msg.to, msg.cc, msg.subject or "", hello + core + "\n\nPlease reply-all in writing.\n\nThanks,\nFo")
