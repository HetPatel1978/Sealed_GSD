"""Regression scenarios for proposals, the approval gate, and written booking confirmations."""

from dataclasses import replace

import pytest

from sealed.core.engine import Engine, SendResult, StaleProposal
from sealed.private import vault
from sealed.private.qualifier import qualify
from tests.helpers import FakeChecker, email, job


class RecordingTransport:
    def __init__(self):
        self.sent = []
        self.change_booking_copy = False
        self.crlf_copy = False

    def send(self, now, msg, approve_preview):
        self.sent.append(msg)
        copy = msg.body + " An extra instruction." if self.change_booking_copy and msg.kind == "booking" else msg.body
        if self.crlf_copy:
            copy = copy.replace("\n", "\r\n")
        return SendResult(True, now, sent_copy=copy)


@pytest.fixture
def eng():
    j = job()
    vault.store(j.id, 40000)
    engine = Engine(j, RecordingTransport(), FakeChecker(),
                    lambda qs, flagged, exclude: qualify(j.id, qs, flagged, exclude),
                    cc="buyer@example.com", tag="SEALED-TEST")
    engine.create(0)
    engine.start(0.1)
    yield engine
    vault.forget(j.id)


def propose(eng):
    eng.on_inbound(10, email("a", 30000))
    eng.on_inbound(11, email("b", 31000))
    eng.on_timer(480, "round2_close")
    assert eng.state == "AWAITING_APPROVAL"
    return eng.proposal.copy()


def confirmation(eng):
    return replace(email("a", 30000, confirms_booking=True), in_reply_to=eng.booking_message)


@pytest.mark.parametrize("revision", [email("a", 35000), email("a", None, declined=True)])
def test_dealer_revision_invalidates_old_approval_and_selects_current_terms(eng, revision):
    old = propose(eng)
    eng.on_inbound(481, revision)
    assert eng.proposal["vendor_id"] == "b" and eng.proposal["proposal_id"] != old["proposal_id"]
    before = len(eng.transport.sent)
    with pytest.raises(StaleProposal):
        eng.approve(482, old)
    assert len(eng.transport.sent) == before


def test_withdrawal_without_an_alternative_clears_the_proposal(eng):
    propose(eng)
    eng.on_inbound(481, email("b", None, declined=True))
    eng.on_inbound(482, email("a", None, declined=True))
    assert eng.state == "NEEDS_HUMAN" and eng.proposal is None


def test_unverified_sender_never_creates_a_proposal_or_triggers_a_reply(eng):
    before = len(eng.transport.sent)
    eng.on_inbound(10, replace(email("a", 28000), from_addr="attacker@example.com"))
    assert len(eng.transport.sent) == before
    assert eng.log.of_type("quote.verified")[-1]["data"]["status"] == "UNVERIFIED"
    eng.on_timer(480, "round2_close")
    assert eng.state == "NEEDS_HUMAN" and eng.proposal is None


def test_ungrounded_parser_output_is_marked_unusable_in_events(eng):
    eng.on_inbound(10, replace(email("a", 28000), text="No price was stated."))
    parsed = eng.log.of_type("quote.parsed")[-1]["data"]
    assert parsed["firm"] is False and parsed["numbers_grounded"] is False
    eng.on_timer(480, "round2_close")
    assert eng.proposal is None


@pytest.mark.parametrize("change", [
    {"price_total": 35000}, {"currency": "CAD"}, {"available": "2026-12-31"},
    {"meets_spec": False}, {"numbers_grounded": False}, {"out_the_door": False},
    {"hedge_words": ("about",)}, {"suspicious": True},
])
def test_booking_confirmation_must_match_every_approved_term(eng, change):
    eng.approve(481, propose(eng))
    inbound = confirmation(eng)
    parsed = replace(inbound.parsed, **change)
    eng.on_inbound(490, replace(inbound, parsed=parsed, text=f"Confirmed total: ${parsed.price_total:,.2f}."))
    assert eng.state == "NEEDS_HUMAN" and not eng.log.of_type("job.completed")
    assert eng.log.of_type("job.needs_human")[-1]["data"]["reason_code"] == "booking_mismatch"


@pytest.mark.parametrize("sender", [None, "attacker@example.com"])
def test_missing_or_wrong_sender_cannot_confirm_a_booking(eng, sender):
    eng.approve(481, propose(eng))
    eng.on_inbound(490, replace(confirmation(eng), from_addr=sender))
    assert eng.state == "BOOKING" and not eng.log.of_type("job.completed")


def test_unrelated_confirmation_thread_cannot_complete_a_booking(eng):
    eng.approve(481, propose(eng))
    eng.on_inbound(490, replace(confirmation(eng), in_reply_to="some-other-message"))
    assert eng.state == "NEEDS_HUMAN" and not eng.log.of_type("job.completed")


def test_matching_written_confirmation_completes_exactly_once(eng):
    eng.approve(481, propose(eng))
    inbound = confirmation(eng)
    eng.on_inbound(490, inbound)
    eng.on_inbound(491, inbound)
    assert eng.state == "COMPLETED" and len(eng.log.of_type("job.completed")) == 1
    assert eng.log.of_type("job.completed")[0]["data"]["best_quote"] == 30000


def test_blocked_booking_pauses_and_keeps_other_dealers_available(eng):
    proposal = propose(eng)
    # A later payload guard failure must not be mistaken for a successfully sent booking.
    class BlockBooking:
        def contains_secret(self, text):
            return "purchase paperwork" in text
    eng.guard._checker = BlockBooking()
    before = len(eng.transport.sent)
    eng.approve(481, proposal)
    assert eng.state == "NEEDS_HUMAN" and eng.booking_message is None
    assert len(eng.transport.sent) == before
    eng.on_inbound(490, confirmation(eng))
    assert not eng.log.of_type("job.completed")


def test_changed_booking_copy_pauses_before_declining_other_dealers(eng):
    proposal = propose(eng)
    eng.transport.change_booking_copy = True
    before = len(eng.transport.sent)
    eng.approve(481, proposal)
    assert eng.state == "NEEDS_HUMAN"
    assert [m.kind for m in eng.transport.sent[before:]] == ["booking"]


def test_mail_line_ending_changes_do_not_pause_a_valid_booking(eng):
    proposal = propose(eng)
    eng.transport.crlf_copy = True
    eng.approve(481, proposal)
    assert eng.state == "BOOKING"
    assert all(e["data"]["matched"] and e["data"]["guard_passed"] for e in eng.log.of_type("audit.checked"))


@pytest.mark.parametrize("change", [
    {"proposal_id": "stale"}, {"vendor_id": "b"}, {"price_total": 30001},
    {"currency": "CAD"}, {"delivery_date": "2026-10-26"},
])
def test_approval_must_echo_every_proposed_term_without_sending_anything(eng, change):
    proposal = propose(eng)
    before = len(eng.transport.sent)
    with pytest.raises(StaleProposal):
        eng.approve(481, proposal | change)
    assert eng.state == "AWAITING_APPROVAL" and len(eng.transport.sent) == before


def test_confirmation_from_a_different_dealer_cannot_complete_the_winner(eng):
    eng.approve(481, propose(eng))
    eng.on_inbound(490, replace(email("b", 30000, confirms_booking=True), in_reply_to=eng.booking_message))
    assert eng.state == "BOOKING" and not eng.log.of_type("job.completed")


def test_confirmation_amount_must_appear_in_the_actual_email(eng):
    eng.approve(481, propose(eng))
    eng.on_inbound(490, replace(confirmation(eng), text="Confirmed. Contact us for the total."))
    assert eng.state == "NEEDS_HUMAN" and not eng.log.of_type("job.completed")


def test_cents_survive_a_complete_proposal_booking_and_confirmation(eng):
    eng.on_inbound(10, email("a", 30000.25))
    eng.on_timer(480, "round2_close")
    eng.approve(481, eng.proposal.copy())
    booking = next(m for m in eng.transport.sent if m.kind == "booking")
    assert "$30,000.25" in booking.body
    eng.on_inbound(490, replace(email("a", 30000.25, confirms_booking=True), in_reply_to=eng.booking_message))
    assert eng.state == "COMPLETED" and eng.log.of_type("job.completed")[-1]["data"]["best_quote"] == 30000.25
