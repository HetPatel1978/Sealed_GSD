"""Strategy engine: the fixed round plan. Pure functions of (job, ledger, what was sent).

There is no ceiling parameter anywhere in this module. Offers are only the job's public target; cited prices
are only real firm written quotes from the ledger.
"""

from sealed.core.ledger import Ledger
from sealed.core.models import Decision, Job, Quote


def money(x: float) -> str:
    return f"${x:,.0f}"


def after_reply(quote: Quote, firm_up_sent: bool, already_flagged: bool) -> Decision | None:
    """Reaction to one written reply in any round."""
    if quote.suspicious and not already_flagged:
        return Decision(quote.dealer_id, "needs_human", "none", reason=(
            "Reply contains an instruction or claim Sealed did not make (e.g. an agreement or payment request). "
            "Sealed did not reply or act on it."))
    if quote.source == "email" and not quote.firm and not quote.declined and not firm_up_sent:
        return Decision(quote.dealer_id, "firm_up", "firm_up", reason=(
            "Reply is not a written out-the-door total for the exact car. Asking once for the total."))
    return None


def call_nudges(job: Job, ledger: Ledger, contacted: set[str], nudged: set[str]) -> list[Decision]:
    """Round 1, after the fixed wait: call dealers that haven't replied in writing. Once each."""
    return [Decision(d.id, "firm_up", "rfq", channel="call", reason=(
                "No written reply after the fixed wait. Call asking the dealer to email an out-the-door total."))
            for d in job.dealers
            if d.id in contacted and d.id not in nudged and not ledger.has_written_reply(d.id) and d.phone]


def round2(job: Job, ledger: Ledger) -> list[Decision]:
    """Round 2: every dealer with a firm quote hears one true fact. Never depends on the ceiling."""
    decisions = []
    for mine in ledger.current_firm():
        low = ledger.lowest_firm(exclude_dealer=mine.dealer_id)
        if low is not None and low.price_total < mine.price_total:
            decisions.append(Decision(mine.dealer_id, "price_match", "price_match", round=2,
                                      own_price=mine.price_total, cited_quote_id=low.quote_id,
                                      cited_price=low.price_total, reason=(
                    f"The lowest written out-the-door total ({money(low.price_total)}) is below this dealer's "
                    f"({money(mine.price_total)}).")))
        elif job.target < mine.price_total:
            decisions.append(Decision(mine.dealer_id, "counter", "counter", round=2, offer=job.target,
                                      own_price=mine.price_total, reason=(
                    "Holds the lowest written total; the target is below it. Ask once at the target.")))
    return decisions


def round2_calls(job: Job, ledger: Ledger, price_match_sent: dict[str, float], replied_after: dict[str, float],
                 called: set[str]) -> list[Decision]:
    """Round 2, after the fixed wait: call dealers that didn't answer the price-match email, with the same fact."""
    out = []
    for dealer_id, sent_at in price_match_sent.items():
        d = job.dealer(dealer_id)
        if dealer_id in called or not d.phone or replied_after.get(dealer_id, -1) > sent_at:
            continue
        low = ledger.lowest_firm(exclude_dealer=dealer_id)
        if low is None:
            continue
        out.append(Decision(dealer_id, "price_match", "price_match", channel="call", round=2,
                            cited_quote_id=low.quote_id, cited_price=low.price_total, reason=(
                "No reply to the round-2 email after the fixed wait. Call with the same true fact.")))
    return out
