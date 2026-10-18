"""Written quotes checked against the dealer's sender address and actual text.

History is retained for evidence and savings. Only the latest verified written reply per dealer can
be used as a current offer; a withdrawal or non-firm revision supersedes an older firm quote.
"""

from collections.abc import Mapping
from email.utils import parseaddr
from math import isfinite

from sealed.core import breakdown as bd
from sealed.core.models import Inbound, Quote
from sealed.core.numbers import numbers_in


def total_grounded(inbound: Inbound) -> bool:
    p = inbound.parsed
    return (p.numbers_grounded and p.price_total is not None and isfinite(p.price_total)
            and p.price_total > 0 and p.price_total in numbers_in(inbound.text))


def latest_written(quotes: list[Quote]) -> list[Quote]:
    """Latest sender-verified email per dealer, including withdrawals and non-firm revisions."""
    latest: dict[str, Quote] = {}
    for q in quotes:
        if q.source == "email" and q.verified:
            prior = latest.get(q.dealer_id)
            if prior is None or q.received_at >= prior.received_at:
                latest[q.dealer_id] = q
    return list(latest.values())


class Ledger:
    def __init__(self, deadline: str, dealer_emails: Mapping[str, str | None], currency: str = "USD"):
        self.deadline, self.currency = deadline, currency
        self.dealer_emails = dict(dealer_emails)
        self.quotes: list[Quote] = []
        self._n = 0

    def verified_sender(self, inbound: Inbound) -> bool:
        expected = self.dealer_emails.get(inbound.dealer_id, "")
        actual = parseaddr(inbound.from_addr or "")[1]
        return bool(inbound.source == "email" and expected and actual
                    and actual.casefold() == parseaddr(expected)[1].casefold())

    def is_firm(self, inbound: Inbound) -> bool:
        p = inbound.parsed
        return (self.verified_sender(inbound) and total_grounded(inbound) and p.out_the_door
                and p.currency == self.currency and p.meets_spec and not p.declined and not p.hedge_words)

    def add(self, reply_id: str, inbound: Inbound, now: float) -> Quote:
        p = inbound.parsed
        self._n += 1
        clean, flags = bd.validate(p.breakdown, inbound.text, p.price_total)
        q = Quote(
            quote_id=f"q_{self._n:02d}", dealer_id=inbound.dealer_id, reply_id=reply_id, source=inbound.source,
            price_total=p.price_total, firm=self.is_firm(inbound), meets_spec=p.meets_spec,
            meets_deadline=None if p.available is None else p.available <= self.deadline, available=p.available,
            hedge_words=tuple(p.hedge_words), declined=p.declined, suspicious=p.suspicious, received_at=now,
            breakdown=clean, breakdown_flags=tuple(flags), verified=self.verified_sender(inbound),
            numbers_grounded=total_grounded(inbound),
        )
        self.quotes.append(q)
        return q

    def get(self, quote_id: str) -> Quote:
        return next(q for q in self.quotes if q.quote_id == quote_id)

    def firm(self) -> list[Quote]:
        """Historical firm quotes, used for the first-quote savings baseline."""
        return [q for q in self.quotes if q.firm and q.meets_deadline is not False]

    def latest_firm(self, dealer_id: str) -> Quote | None:
        return next((q for q in self.current_firm() if q.dealer_id == dealer_id), None)

    def current_firm(self) -> list[Quote]:
        return [q for q in latest_written(self.quotes) if q.firm and q.meets_deadline is not False]

    def lowest_firm(self, exclude_dealer: str | None = None) -> Quote | None:
        pool = [q for q in self.current_firm() if q.dealer_id != exclude_dealer]
        return min(pool, key=lambda q: (q.price_total, q.received_at)) if pool else None

    def has_written_reply(self, dealer_id: str) -> bool:
        return any(q.dealer_id == dealer_id and q.verified for q in self.quotes)

    def declined(self, dealer_id: str) -> bool:
        mine = [q for q in latest_written(self.quotes) if q.dealer_id == dealer_id]
        return bool(mine) and mine[-1].declined
