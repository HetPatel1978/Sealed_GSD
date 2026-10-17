"""Quote ledger. Code decides what is firm: a WRITTEN (email) out-the-door total for the exact spec.

A price heard on a call, or relayed by Fo, is stored but never firm.
"""

from sealed.core import breakdown as bd
from sealed.core.models import Inbound, Quote


def is_firm(source: str, parsed) -> bool:
    return (source == "email" and parsed.price_total is not None and parsed.out_the_door
            and parsed.meets_spec and not parsed.declined and not parsed.hedge_words)


class Ledger:
    def __init__(self, deadline: str):
        self.deadline = deadline
        self.quotes: list[Quote] = []
        self._n = 0

    def add(self, reply_id: str, inbound: Inbound, now: float) -> Quote:
        p = inbound.parsed
        self._n += 1
        clean, flags = bd.validate(p.breakdown, inbound.text, p.price_total)
        q = Quote(
            quote_id=f"q_{self._n:02d}", dealer_id=inbound.dealer_id, reply_id=reply_id, source=inbound.source,
            price_total=p.price_total, firm=is_firm(inbound.source, p), meets_spec=p.meets_spec,
            meets_deadline=None if p.available is None else p.available <= self.deadline, available=p.available,
            hedge_words=tuple(p.hedge_words), declined=p.declined, suspicious=p.suspicious, received_at=now,
            breakdown=clean, breakdown_flags=tuple(flags),
        )
        self.quotes.append(q)
        return q

    def get(self, quote_id: str) -> Quote:
        return next(q for q in self.quotes if q.quote_id == quote_id)

    def firm(self) -> list[Quote]:
        return [q for q in self.quotes if q.firm and q.meets_deadline is not False]

    def latest_firm(self, dealer_id: str) -> Quote | None:
        mine = [q for q in self.firm() if q.dealer_id == dealer_id]
        return mine[-1] if mine else None

    def current_firm(self) -> list[Quote]:
        """Each dealer's latest firm quote."""
        return [q for d in dict.fromkeys(q.dealer_id for q in self.firm()) if (q := self.latest_firm(d))]

    def lowest_firm(self, exclude_dealer: str | None = None) -> Quote | None:
        pool = [q for q in self.current_firm() if q.dealer_id != exclude_dealer]
        return min(pool, key=lambda q: (q.price_total, q.received_at)) if pool else None

    def has_written_reply(self, dealer_id: str) -> bool:
        return any(q.dealer_id == dealer_id and q.source == "email" for q in self.quotes)

    def declined(self, dealer_id: str) -> bool:
        mine = [q for q in self.quotes if q.dealer_id == dealer_id and q.source == "email"]
        return bool(mine) and mine[-1].declined
