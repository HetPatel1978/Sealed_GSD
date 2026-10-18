"""Qualifier: the only decision that uses the ceiling. Runs after the fixed rounds.

Output is `propose` (with a quote) or `needs_human`. It never returns the ceiling or a gap to it.
Rule for flagged dealers (suspicious instruction): they are proposed only if no unflagged firm quote
qualifies; the flag is shown on the proposal either way.
"""

from dataclasses import dataclass

from sealed.core.ledger import latest_written
from sealed.core.models import Quote
from sealed.private import vault


@dataclass(frozen=True)
class QualifierResult:
    action: str                          # "propose" | "needs_human"
    quote: Quote | None = None           # the proposed quote, or the closest one for needs_human
    flags: tuple[str, ...] = ()
    reason_code: str | None = None       # for needs_human: no_qualifying_quote | all_declined
    reason: str = ""


def qualify(job_id: str, quotes: list[Quote], flagged_dealers: set[str], exclude_quote_ids: set[str] = frozenset()
            ) -> QualifierResult:
    ceiling = vault._read(job_id)
    current = latest_written(quotes)
    usable = [q for q in current
              if q.firm and q.numbers_grounded and q.source == "email" and q.meets_spec and q.meets_deadline is not False
              and not q.declined and q.price_total is not None and q.quote_id not in exclude_quote_ids]
    if not usable:
        return QualifierResult("needs_human", reason_code="all_declined" if current and all(q.declined for q in current)
                               else "no_qualifying_quote", reason="No firm written out-the-door quote to propose.")

    def rank(q: Quote):
        return (q.price_total, q.available or "9999-12-31", q.received_at)

    within = [q for q in usable if q.price_total <= ceiling]
    clean = [q for q in within if q.dealer_id not in flagged_dealers]
    pool = clean or within
    if pool:
        best = min(pool, key=rank)
        flags = ("suspicious_instruction",) if best.dealer_id in flagged_dealers else ()
        why = "Lowest qualifying firm written quote." if not flags else \
            "Only qualifying firm quote comes from a flagged dealer; review the flag."
        return QualifierResult("propose", best, flags, reason=why)
    closest = min(usable, key=rank)
    flags = ("suspicious_instruction",) if closest.dealer_id in flagged_dealers else ()
    return QualifierResult("needs_human", closest, flags, "no_qualifying_quote",
                           "No firm quote is good enough to propose. The closest one is shown.")
