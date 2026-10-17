"""Builds outbound messages as two parts: the instruction we give Fo, and the exact email the seller reads.

Nothing here ever takes a budget, ceiling or price we'd pay as input.
"""

import secrets
from dataclasses import dataclass

TAG_PREFIX = "SEALED-"
BODY_START = "<<<EMAIL_BODY"
BODY_END = "EMAIL_BODY>>>"


@dataclass(frozen=True)
class OutboundEmail:
    tag: str
    to: str
    cc: str
    subject: str
    email_body: str
    fo_instruction: str


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


def build_quote_request(to: str, cc: str, item: str, tag: str | None = None) -> OutboundEmail:
    tag = tag or new_tag()
    subject = f"Quote request: {item} [{tag}]"
    email_body = (
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
    return OutboundEmail(tag, to, cc, subject, email_body, wrap_for_fo(to, cc, subject, email_body))
