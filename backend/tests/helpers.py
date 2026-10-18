from sealed.core.models import Dealer, Inbound, Job, ParsedReply
from sealed.core.ledger import Ledger


class FakeChecker:
    """Stands in for the secret checker: flags only the literal marker 'SECRET'."""

    def contains_secret(self, text: str) -> bool:
        return "SECRET" in text


def job(target: float = 29500.0) -> Job:
    return Job(id="job_x", item="2026 Honda Civic Sport", details="2026 Honda Civic Sport sedan, Crystal Black Pearl",
               zip="19104", buyer="Arjun", timeline="within the next four weeks", deadline="2026-11-30",
               target=target, dealers=(Dealer("a", "Alpha Motors", "a@x.example", "+1-555-0001"),
                                       Dealer("b", "Bravo Auto", "b@x.example", "+1-555-0002"),
                                       Dealer("c", "Charlie Cars", "c@x.example", None)))


def email(dealer: str, price: float | None, otd: bool = True, **kw) -> Inbound:
    text = f"Out-the-door total: ${price:,.2f}." if price is not None else "No quote available."
    return Inbound(dealer, "email", text,
                   ParsedReply(price, otd, available=kw.pop("available", "2026-10-25"), **kw),
                   from_addr=f"{dealer}@x.example")


def ledger() -> Ledger:
    j = job()
    return Ledger(j.deadline, {d.id: d.email for d in j.dealers}, j.currency)


def call(dealer: str, price: float) -> Inbound:
    return Inbound(dealer, "call", "summary", ParsedReply(price, True, hedge_words=("about",)))
