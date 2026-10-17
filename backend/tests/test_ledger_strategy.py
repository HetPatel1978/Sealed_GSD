import inspect

from sealed.core import strategy
from sealed.core.ledger import Ledger
from tests.helpers import call, email, job


def test_only_written_out_the_door_totals_are_firm():
    led = Ledger("2026-11-30")
    assert led.add("r1", email("a", 30000.0), 1).firm
    assert not led.add("r2", call("a", 29000.0), 2).firm                         # spoken
    assert not led.add("r3", email("b", 27800.0, otd=False), 3).firm             # MSRP / plus fees
    assert not led.add("r4", email("b", 30000.0, hedge_words=("about",)), 4).firm
    assert not led.add("r5", email("c", None, declined=True), 5).firm
    assert not led.add("r6", email("c", 30000.0, meets_spec=False), 6).firm      # wrong trim
    late = led.add("r7", email("c", 29000.0, available="2026-12-15"), 7)
    assert late.firm and late.meets_deadline is False and late not in led.firm()


def test_round2_cites_lowest_written_quote_from_another_dealer_and_counters_at_target():
    j, led = job(), Ledger("2026-11-30")
    led.add("r1", email("a", 31240.0), 1)
    led.add("r2", email("b", 30690.0), 2)
    led.add("r3", call("a", 29000.0), 3)          # spoken, must never be cited
    ds = {d.dealer_id: d for d in strategy.round2(j, led)}
    assert ds["a"].action == "price_match" and ds["a"].cited_price == 30690.0 and ds["a"].offer is None
    assert ds["b"].action == "counter" and ds["b"].offer == j.target and ds["b"].cited_quote_id is None


def test_no_counter_when_target_is_not_below_lowest():
    led = Ledger("2026-11-30")
    led.add("r1", email("a", 29000.0), 1)
    assert strategy.round2(job(target=29500.0), led) == []


def test_offers_are_only_the_target():
    j, led = job(), Ledger("2026-11-30")
    for i, (d, p) in enumerate([("a", 31000.0), ("b", 30500.0), ("c", 30800.0)]):
        led.add(f"r{i}", email(d, p), i)
    assert all(d.offer in (None, j.target) for d in strategy.round2(j, led))


def test_call_nudges_only_silent_dealers_with_a_phone_once():
    j, led = job(), Ledger("2026-11-30")
    led.add("r1", email("a", 31000.0), 1)
    ds = strategy.call_nudges(j, led, contacted={"a", "b", "c"}, nudged=set())
    assert [d.dealer_id for d in ds] == ["b"]                       # a replied, c has no phone
    assert ds[0].channel == "call" and ds[0].kind == "rfq"
    assert strategy.call_nudges(j, led, {"a", "b", "c"}, nudged={"b"}) == []


def test_after_reply_flags_suspicious_and_firms_up_once():
    led = Ledger("2026-11-30")
    trap = led.add("r1", email("a", 30690.0, suspicious=True), 1)
    assert strategy.after_reply(trap, False, False).action == "needs_human"
    vague = led.add("r2", email("b", 27800.0, otd=False), 2)
    assert strategy.after_reply(vague, False, False).kind == "firm_up"
    assert strategy.after_reply(vague, True, False) is None


def test_strategy_has_no_ceiling_input():
    for name, fn in inspect.getmembers(strategy, inspect.isfunction):
        params = " ".join(inspect.signature(fn).parameters)
        assert "ceil" not in params and "budget" not in params and "max" not in params, name
