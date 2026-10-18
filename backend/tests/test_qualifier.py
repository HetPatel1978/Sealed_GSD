import pytest

from sealed.private import vault
from sealed.private.qualifier import qualify
from tests.helpers import call, email, ledger


@pytest.fixture
def led():
    vault.store("job_q", 31000.0)
    yield ledger()
    vault.forget("job_q")


def test_proposes_lowest_firm_written_quote_within_ceiling(led):
    led.add("r1", email("a", 30900.0), 1)
    led.add("r2", email("b", 30500.0), 2)
    led.add("r3", call("c", 29000.0), 3)          # spoken: never proposed
    res = qualify("job_q", led.quotes, set())
    assert res.action == "propose" and res.quote.dealer_id == "b" and res.flags == ()


def test_flagged_dealer_not_proposed_when_an_unflagged_quote_qualifies(led):
    led.add("r1", email("a", 30900.0), 1)
    led.add("r2", email("b", 30000.0, suspicious=True), 2)     # cheaper but flagged
    res = qualify("job_q", led.quotes, {"b"})
    assert res.action == "propose" and res.quote.dealer_id == "a" and res.flags == ()


def test_flagged_dealer_proposed_with_flag_when_it_is_the_only_qualifying_quote(led):
    led.add("r1", email("a", 33000.0), 1)                      # above ceiling
    led.add("r2", email("b", 30000.0, suspicious=True), 2)
    res = qualify("job_q", led.quotes, {"b"})
    assert res.action == "propose" and res.quote.dealer_id == "b" and res.flags == ("suspicious_instruction",)


def test_needs_human_when_nothing_qualifies_shows_closest_without_a_gap(led):
    led.add("r1", email("a", 33000.0), 1)
    led.add("r2", email("b", 32000.0), 2)
    res = qualify("job_q", led.quotes, set())
    assert res.action == "needs_human" and res.reason_code == "no_qualifying_quote"
    assert res.quote.dealer_id == "b"
    assert "31000" not in repr(res) and "31,000" not in repr(res) and "gap" not in repr(res).lower()


def test_rejected_quotes_are_excluded(led):
    a = led.add("r1", email("a", 30000.0), 1)
    led.add("r2", email("b", 30500.0), 2)
    assert qualify("job_q", led.quotes, set(), {a.quote_id}).quote.dealer_id == "b"


def test_latest_price_replaces_a_cheaper_historical_quote(led):
    led.add("old", email("a", 30000), 1)
    led.add("new", email("a", 33000), 2)
    res = qualify("job_q", led.quotes, set())
    assert res.action == "needs_human" and res.quote.price_total == 33000


def test_latest_withdrawal_makes_a_dealer_ineligible(led):
    led.add("old", email("a", 30000), 1)
    led.add("withdrawn", email("a", None, declined=True), 2)
    res = qualify("job_q", led.quotes, set())
    assert res.action == "needs_human" and res.reason_code == "all_declined" and res.quote is None


def test_excluding_latest_quote_does_not_revive_an_older_quote(led):
    led.add("old", email("a", 30000), 1)
    latest = led.add("new", email("a", 30500), 2)
    res = qualify("job_q", led.quotes, set(), {latest.quote_id})
    assert res.action == "needs_human" and res.quote is None
