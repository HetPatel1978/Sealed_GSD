import re
from dataclasses import replace

import pytest

from sealed.core.guard import Guard
from sealed.core.models import Decision
from sealed.fo import messages
from tests.helpers import FakeChecker, job

J = job()
DECISIONS = [
    Decision("a", "rfq", "rfq"),
    Decision("a", "firm_up", "firm_up"),
    Decision("a", "firm_up", "rfq", channel="call"),
    Decision("a", "price_match", "price_match", round=2, own_price=31240.0, cited_quote_id="q_05", cited_price=30690.0),
    Decision("a", "price_match", "price_match", channel="call", round=2, cited_quote_id="q_05", cited_price=30690.0),
    Decision("a", "counter", "counter", round=2, offer=29500.0, own_price=30690.0),
    Decision("a", "propose", "cancel", round=2),
]


def build(d, price=None):
    return messages.build(J, d, "m_01", "SEALED-TEST0001", "buyer@example.com", approved_price=price)


@pytest.mark.parametrize("d", DECISIONS, ids=lambda d: f"{d.kind}-{d.channel}")
def test_every_template_passes_the_guard_and_discloses_ai(d):
    msg = build(d)
    res = Guard(FakeChecker()).check(msg)
    assert res.passed, res.rules
    assert messages.DISCLOSURE in msg.body
    assert "Het" not in msg.body                       # never written as the user


def test_booking_needs_approval_for_commitment_words():
    d = Decision("a", "propose", "booking", round=2)
    approved = build(d, price=30450.0)
    assert approved.booking_approved and Guard(FakeChecker()).check(approved).passed
    unapproved = replace(approved, booking_approved=False)
    rule = {r["rule"]: r for r in Guard(FakeChecker()).check(unapproved).rules}["commitment_words"]
    assert not rule["passed"]


def test_guard_blocks_secret_limit_language_ungrounded_prices_and_commitments():
    g = Guard(FakeChecker())
    base = build(DECISIONS[3])

    def failed(body):
        return {r["rule"] for r in g.check(replace(base, body=body)).rules if not r["passed"]}

    assert "secret_checker" in failed(base.body + " SECRET")
    assert "limit_language" in failed(base.body + " That's above our budget.")
    assert "leverage_grounded" in failed(base.body + " Another dealer offered $29,999.")
    assert "commitment_words" in failed(base.body + " We accept your offer.")
    assert "safety_line" in failed(base.body.replace(messages.DISCLOSURE, "I'm Het"))


def test_preview_must_contain_our_exact_text():
    g, msg = Guard(FakeChecker()), build(DECISIONS[0])
    assert g.check(msg, preview=f"Here's the draft:\n{msg.body}\nGood to send?").passed
    res = g.check(msg, preview=msg.body.replace("out-the-door total", "best price"))
    assert not res.passed and res.stage == "fo_preview"


def test_call_scripts_only_state_ledger_prices():
    msg = build(DECISIONS[4])
    assert re.findall(r"\$[\d,]+", msg.body) == ["$30,690"]
    assert "Do not agree to anything" in msg.fo_instruction
