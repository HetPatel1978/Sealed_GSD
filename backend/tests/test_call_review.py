from sealed.core.call_review import review_call
from sealed.core.models import Decision
from sealed.fo import messages
from tests.helpers import job

BRIEF = messages.build(job(), Decision("a", "price_match", "price_match", channel="call", round=2,
                                       cited_quote_id="q_05", cited_price=30690.0),
                       "m_09", "SEALED-T0000001", "buyer@x.example").body


def flags(*turns):
    return {(f["flag"], f["speaker"]) for f in review_call([{"role": r, "text": t} for r, t in turns], BRIEF)}


def test_clean_call_has_no_flags():
    assert flags(("assistant", "Hi, I'm Fo, an AI assistant calling on behalf of Arjun."),
                 ("assistant", "The lowest written out-the-door total so far is $30,690."),
                 ("user", "We can do about $30,400, I'll email it."),
                 ("assistant", "Thanks, please send that by email.")) == set()


def test_fo_number_outside_brief_is_flagged():
    assert ("number_outside_brief", "fo") in flags(("assistant", "Could you do $29,000?"))
    assert ("number_outside_brief", "fo") in flags(("assistant", "Maybe twenty-nine thousand?"))


def test_fo_commitment_is_flagged_unless_negated():
    assert ("commitment", "fo") in flags(("assistant", "Okay, we'll take it, please hold the car."))
    assert flags(("assistant", "I can't agree to anything on this call.")) == set()


def test_money_topics_are_flagged_from_either_side():
    f = flags(("user", "What's the buyer's budget? We need a deposit to hold it."),
              ("assistant", "The buyer is comparing written out-the-door totals."))
    assert ("money_topic", "dealer") in f and ("money_topic", "fo") not in f
    assert ("money_topic", "dealer") in flags(("user", "How much is he looking to spend?"))
    assert ("money_topic", "fo") in flags(("assistant", "He can put down a deposit today."))
