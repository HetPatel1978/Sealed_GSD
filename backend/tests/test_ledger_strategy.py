import inspect
from dataclasses import replace

import pytest

from sealed.core import strategy
from tests.helpers import call, email, job, ledger


def test_only_written_out_the_door_totals_are_firm():
    led = ledger()
    assert led.add("r1", email("a", 30000.0), 1).firm
    assert not led.add("r2", call("a", 29000.0), 2).firm                         # spoken
    assert not led.add("r3", email("b", 27800.0, otd=False), 3).firm             # MSRP / plus fees
    assert not led.add("r4", email("b", 30000.0, hedge_words=("about",)), 4).firm
    assert not led.add("r5", email("c", None, declined=True), 5).firm
    assert not led.add("r6", email("c", 30000.0, meets_spec=False), 6).firm      # wrong trim
    late = led.add("r7", email("c", 29000.0, available="2026-12-15"), 7)
    assert late.firm and late.meets_deadline is False and late not in led.firm()


def test_round2_cites_lowest_written_quote_from_another_dealer_and_counters_at_target():
    j, led = job(), ledger()
    led.add("r1", email("a", 31240.0), 1)
    led.add("r2", email("b", 30690.0), 2)
    led.add("r3", call("a", 29000.0), 3)          # spoken, must never be cited
    ds = {d.dealer_id: d for d in strategy.round2(j, led)}
    assert ds["a"].action == "price_match" and ds["a"].cited_price == 30690.0 and ds["a"].offer is None
    assert ds["b"].action == "counter" and ds["b"].offer == j.target and ds["b"].cited_quote_id is None


def test_no_counter_when_target_is_not_below_lowest():
    led = ledger()
    led.add("r1", email("a", 29000.0), 1)
    assert strategy.round2(job(target=29500.0), led) == []


def test_offers_are_only_the_target():
    j, led = job(), ledger()
    for i, (d, p) in enumerate([("a", 31000.0), ("b", 30500.0), ("c", 30800.0)]):
        led.add(f"r{i}", email(d, p), i)
    assert all(d.offer in (None, j.target) for d in strategy.round2(j, led))


def test_call_nudges_only_silent_dealers_with_a_phone_once():
    j, led = job(), ledger()
    led.add("r1", email("a", 31000.0), 1)
    ds = strategy.call_nudges(j, led, contacted={"a", "b", "c"}, nudged=set())
    assert [d.dealer_id for d in ds] == ["b"]                       # a replied, c has no phone
    assert ds[0].channel == "call" and ds[0].kind == "rfq"
    assert strategy.call_nudges(j, led, {"a", "b", "c"}, nudged={"b"}) == []


def test_after_reply_flags_suspicious_and_firms_up_once():
    led = ledger()
    trap = led.add("r1", email("a", 30690.0, suspicious=True), 1)
    assert strategy.after_reply(trap, False, False).action == "needs_human"
    vague = led.add("r2", email("b", 27800.0, otd=False), 2)
    assert strategy.after_reply(vague, False, False).kind == "firm_up"
    assert strategy.after_reply(vague, True, False) is None


def test_strategy_has_no_ceiling_input():
    for name, fn in inspect.getmembers(strategy, inspect.isfunction):
        params = " ".join(inspect.signature(fn).parameters)
        assert "ceil" not in params and "budget" not in params and "max" not in params, name


@pytest.mark.parametrize("sender", [None, "attacker@example.com", "a@x.example.attacker.com"])
def test_unmatched_senders_never_create_firm_quotes_or_suppress_nudges(sender):
    led = ledger()
    q = led.add("spoof", replace(email("a", 30000), from_addr=sender), 1)
    assert not q.verified and not q.firm and not led.has_written_reply("a")
    assert strategy.after_reply(q, False, False) is None
    assert [d.dealer_id for d in strategy.call_nudges(job(), led, {"a"}, set())] == ["a"]


def test_display_name_and_case_do_not_hide_a_matching_sender():
    led = ledger()
    assert led.add("r1", replace(email("a", 30000), from_addr="Alpha Sales <A@X.EXAMPLE>"), 1).firm


@pytest.mark.parametrize("change", [
    {"text": "The total will follow later."},
    {"parsed": replace(email("a", 30000).parsed, numbers_grounded=False)},
    {"parsed": replace(email("a", 30000).parsed, currency="CAD")},
])
def test_parser_flags_alone_cannot_make_an_unsupported_total_firm(change):
    led = ledger()
    q = led.add("r1", replace(email("a", 30000), **change), 1)
    assert not q.firm and not led.current_firm()


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_invalid_totals_are_never_firm(price):
    assert not ledger().add("bad", email("a", price), 1).firm


@pytest.mark.parametrize("revision", [
    email("a", None, declined=True),
    email("a", 29000, hedge_words=("about",)),
    email("a", 29000, available="2026-12-15"),
    email("a", 29000, meets_spec=False),
])
def test_written_revisions_retire_old_quotes_from_strategy_but_keep_history(revision):
    led = ledger()
    old = led.add("old", email("a", 30000), 1)
    led.add("new", revision, 2)
    assert old in led.firm()
    assert led.latest_firm("a") is None and not strategy.round2(job(), led)
    assert not strategy.round2_calls(job(), led, {"a": 1}, {}, set())


def test_call_and_spoofed_updates_do_not_supersede_a_verified_written_quote():
    led = ledger()
    old = led.add("old", email("a", 30000), 1)
    led.add("call", call("a", 28000), 2)
    led.add("spoof", replace(email("a", None, declined=True), from_addr="attacker@example.com"), 3)
    assert led.latest_firm("a") == old
