from dataclasses import replace

import pytest

from sealed.core.engine import Engine, SendResult
from sealed.core.models import Dealer, Inbound, ParsedReply
from sealed.sim.dealers import Response, SimDealer
from sealed.sim.run import default_dealers, default_job, run
from tests.helpers import FakeChecker
from sealed.sim.email import compose

PENN_ADDR = "sales@penn-motors.example"


def test_dealer_needs_an_email_or_a_phone():
    with pytest.raises(ValueError):
        Dealer("x", "Nowhere Motors", None, None)
    assert Dealer("p", "Phone Only", None, "+1-555-0199").email is None
    assert Dealer("e", "Email Only", "e@x.example").phone is None
    with pytest.raises(ValueError):
        Dealer("s", "Bad Source", "s@x.example", source="web")


def _run_with_phone_only_dealers():
    base = default_job()
    penn = Dealer("v_penn", "Penn Motors", None, "+1-555-0198", source="discovery",
                  source_urls=("https://penn-motors.example/contact",))
    quiet = Dealer("v_quiet", "Quiet Auto", None, "+1-555-0197")
    job = replace(base, dealers=base.dealers + (penn, quiet))
    # Penn answers the call with a written total from an address we didn't have; above the ceiling, so never proposed.
    sims = default_dealers(job) | {
        "v_penn": SimDealer(penn, {("rfq", "call"): [Response(
            40, "email", "Out-the-door total for the Civic Sport is $32,900 including all fees and taxes.",
            ParsedReply(32900.0, True, available="2026-10-25"), from_addr=PENN_ADDR, contact_verified=True)]}),
        "v_quiet": SimDealer(quiet, {}),
    }
    return run(job=job, dealers=sims)


def _sent(eng, dealer_id):
    return [e["data"] for e in eng.log.events if e["type"] == "message.sent" and e["vendor_id"] == dealer_id]


def test_phone_only_dealer_gets_the_round1_request_as_a_call_that_does_not_claim_an_email():
    eng = _run_with_phone_only_dealers()
    first = _sent(eng, "v_penn")[0]
    assert (first["kind"], first["channel"]) == ("rfq", "call")
    assert "We emailed" not in first["body"] and "first contact" in first["body"]
    assert "reply to the email we sent" not in first["body"]


def test_phone_only_dealer_is_then_emailed_at_the_address_it_wrote_from_and_verified():
    eng = _run_with_phone_only_dealers()
    emails = [m for m in _sent(eng, "v_penn")[1:] if m["channel"] == "email"]
    assert {m["kind"] for m in emails} >= {"price_match", "cancel"}
    assert all(m["to"] == PENN_ADDR for m in emails)
    verified = [e["data"]["status"] for e in eng.log.events
                if e["type"] == "quote.verified" and e["vendor_id"] == "v_penn"]
    assert verified and verified[0] == "VERIFIED"


def test_silent_phone_only_dealer_gets_one_call_and_no_email():
    eng = _run_with_phone_only_dealers()
    quiet = _sent(eng, "v_quiet")
    assert [(m["kind"], m["channel"]) for m in quiet] == [("rfq", "call")]


def test_job_created_lists_phone_only_dealers_with_their_source():
    eng = _run_with_phone_only_dealers()
    vendors = {v["id"]: v for v in eng.log.events[0]["data"]["vendors"]}
    assert vendors["v_penn"]["email"] is None and vendors["v_penn"]["phone"]
    assert vendors["v_penn"]["source"] == {"kind": "discovery", "urls": ["https://penn-motors.example/contact"]}
    assert vendors["v_northline"]["source"] == {"kind": "manual"}


@pytest.fixture
def phone_engine():
    class Transport:
        def send(self, now, msg, approve_preview):
            draft = compose(msg) if msg.channel == "email" else None
            return SendResult(True, now, sent_copy=draft.body if draft else None, draft=draft)

    job = replace(default_job(), dealers=(Dealer("p", "Penn Motors", None, "+1-555-0198"),
                                         Dealer("q", "Quiet Auto", None, "+1-555-0197")))
    eng = Engine(job, Transport(), FakeChecker(), qualifier=None, cc="buyer@example.com", tag="PHONE-TEST")
    eng.start(0)
    return eng


def _reply(eng, **changes):
    call = next(m for m in eng.outbound.values() if m.dealer_id == "p")
    reply = Inbound("p", "email", "Out-the-door total: $32,900.",
                    ParsedReply(32900, True, available="2026-10-25"),
                    in_reply_to=call.message_id, from_addr=PENN_ADDR, contact_verified=True)
    return replace(reply, **changes)


@pytest.mark.parametrize("changes", [
    {"contact_verified": False}, {"in_reply_to": None}, {"in_reply_to": "unrelated"}, {"from_addr": None},
])
def test_unknown_senders_need_trusted_attribution_and_a_link_to_the_call(phone_engine, changes):
    eng = phone_engine
    eng.on_inbound(10, _reply(eng, **changes))
    assert eng.email_to("p") is None
    assert not eng.ledger.current_firm()
    assert eng.log.of_type("quote.verified")[-1]["data"]["status"] == "UNVERIFIED"


def test_reply_linked_to_another_dealers_call_cannot_register_an_address(phone_engine):
    eng = phone_engine
    other = next(m for m in eng.outbound.values() if m.dealer_id == "q")
    eng.on_inbound(10, _reply(eng, in_reply_to=other.message_id))
    assert eng.email_to("p") is None and not eng.ledger.current_firm()


def test_attributed_sender_is_learned_once_and_cannot_be_replaced_by_a_later_sender(phone_engine):
    eng = phone_engine
    eng.on_inbound(10, _reply(eng))
    original = eng.ledger.latest_firm("p")
    assert eng.email_to("p") == PENN_ADDR and original is not None
    eng.on_inbound(11, _reply(eng, from_addr="attacker@example.com"))
    assert eng.email_to("p") == PENN_ADDR and eng.ledger.latest_firm("p") == original
    assert eng.log.of_type("quote.verified")[-1]["data"]["status"] == "UNVERIFIED"
