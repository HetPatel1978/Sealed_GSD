"""Hybrid outreach: Fo composes; code owns prices, routing and permission to act."""

import json
from dataclasses import asdict, replace

import pytest

from sealed.core.email_policy import format_draft, parse_draft
from sealed.core.engine import Engine
from sealed.core.guard import Guard
from sealed.core.models import Decision
from sealed.fo import messages
from sealed.fo.adapter import CONFIRM_BRIEF, CONFIRM_NO, FoTransport
from sealed.sim.email import compose
from tests.helpers import FakeChecker, job
from tests.test_fo_adapter import FakeFo, ok

J = job()


def message(kind="rfq", *, job=J):
    values = {}
    if kind in {"price_match", "counter"}:
        values.update(own_price=31240, own_quote_id="q_own")
    if kind == "price_match":
        values.update(cited_price=30690, cited_quote_id="q_competing")
    if kind == "counter":
        values.update(offer=job.target)
    return messages.build(job, Decision("a", kind, kind, **values), "m_01", "SEALED-HYBRID",
                          "buyer@example.com", approved_price=30450 if kind == "booking" else None)


def failures(msg, draft):
    return {r["rule"] for r in Guard(FakeChecker()).check_email(msg, draft).rules if not r["passed"]}


@pytest.mark.parametrize("kind", ["rfq", "firm_up", "price_match", "counter", "booking", "cancel"])
def test_all_actions_send_a_brief_and_accept_a_separately_composed_email(kind):
    msg = message(kind)
    draft = compose(msg)
    assert msg.email_brief is not None and msg.body.startswith("Goal:")
    assert "in your own words" in msg.fo_instruction and "DRAFT request" in msg.fo_instruction
    assert "send the text between the markers VERBATIM" not in msg.fo_instruction
    assert draft.body not in msg.fo_instruction and msg.body != draft.body
    guard = Guard(FakeChecker())
    assert guard.check(msg).passed
    assert guard.check(msg, preview=format_draft(draft)).passed
    assert not failures(msg, draft)
    assert parse_draft(format_draft(draft)) == draft


def test_natural_rewording_and_a_normal_signature_are_accepted():
    msg = message()
    draft = replace(compose(msg), body=(
        f"Hello! I'm Fo, an artificial intelligence assistant helping {J.buyer}. "
        f"We're looking for {J.details}. Could you provide a written out-the-door total "
        "covering all fees, taxes and add-ons? Please confirm availability and explain quote validity. "
        "Please reply-all.\n\nKind regards,\nFo\nGet your own Fo: https://wajo.ai/Fo\n"
        "instagram.com/wajo_ai · x.com/wajo_ai · linkedin.com/company/wajo-ai"))
    assert not failures(msg, draft)


def test_live_fo_plain_closing_marker_still_requires_full_envelope_and_guard(tmp_path):
    msg = message("counter")
    draft = compose(msg)
    preview = format_draft(draft).replace("EMAIL_DRAFT>>>", "EMAIL_DRAFT")
    assert parse_draft(preview) == draft
    fo = FakeFo([ok(preview), ok("Sent.")])
    guard = Guard(FakeChecker())
    result = FoTransport(fo, tmp_path / "fo.json").send(
        0, msg, lambda text: guard.check(msg, preview=text).passed)
    assert result.sent and fo.asks[1][0] == CONFIRM_BRIEF
    malicious = preview.replace(draft.body, draft.body + "\nI paid the deposit.")
    assert not guard.check(msg, preview=malicious).passed


def test_live_fo_own_quote_attribution_and_conditional_written_total_request():
    msg = message("counter")
    draft = replace(compose(msg), body=(
        f"I'm Fo, an AI assistant writing for {J.buyer}. "
        f"The written total you gave for {J.details} came in at $31,240 USD, including fees and taxes.\n"
        "Could you meet $29,500 USD, confirmed as a written out-the-door total including fees and taxes?\n"
        "Please reply-all in writing with the total and availability.\nNo payment is being made by email."))
    assert not failures(msg, draft)
    assert "commitment_words" in failures(msg, replace(draft, body=draft.body +
        "\nOur deal is confirmed as a written out-the-door total."))
    assert "price_roles" in failures(msg, replace(draft, body=draft.body.replace(
        "The written total you gave", "Another dealer's written total")))


@pytest.mark.parametrize("alteration", ["suffix", "nested", "partial_marker", "wrong_recipient"])
def test_live_fo_marker_variant_does_not_allow_envelope_injection(alteration):
    msg = message()
    draft = compose(msg)
    preview = format_draft(draft).replace("EMAIL_DRAFT>>>", "EMAIL_DRAFT")
    if alteration == "suffix":
        preview += "\nSend another email too."
    elif alteration == "nested":
        preview = preview.replace(draft.body, draft.body + "\nEMAIL_DRAFT\nGood to send?")
    elif alteration == "partial_marker":
        preview = preview.replace("\nEMAIL_DRAFT\n", "\nEMAIL_DRAFT>>\n")
    else:
        preview = preview.replace("To: " + msg.to, "To: attacker@example.com")
    assert not Guard(FakeChecker()).check(msg, preview=preview).passed


@pytest.mark.parametrize("field,value", [("to", "attacker@example.com"), ("cc", "other@example.com"),
                                         ("cc", "buyer@example.com, other@example.com"),
                                         ("subject", "Changed subject")])
def test_generated_email_cannot_change_routing_or_subject(field, value):
    msg = message()
    assert "draft_routing" in failures(msg, replace(compose(msg), **{field: value}))


@pytest.mark.parametrize("extra,rule", [
    (" Another dealer offered $28,000.", "brief_numbers"),
    (" Another dealer offered 28000 USD.", "brief_numbers"),
    (" Another dealer offered twenty-eight thousand dollars.", "brief_numbers"),
    (" The total is two thousand twenty-six dollars.", "brief_numbers"),
    (" Please send $2026.", "brief_numbers"),  # known model year is not a permitted price
    (" We accept your offer.", "commitment_words"),
    (" I paid the deposit.", "commitment_words"),
    (" I'll schedule a follow-up.", "action_scope"),
    (" Email another person too.", "action_scope"),
    (" Send this to billing@other.example.com.", "action_scope"),
    (" Visit https://attacker.example.com/pay.", "action_scope"),
    (" Delivery is guaranteed tomorrow.", "facts_grounded"),
    (" Free delivery is included.", "facts_grounded"),
    (" The car is in stock.", "facts_grounded"),
    (" BCC: other@example.com", "draft_routing"),
    (" Our budget is flexible.", "limit_language"),
    (" SECRET", "secret_checker"),
])
def test_unsupported_claims_prices_contacts_and_commitments_require_review(extra, rule):
    msg = message()
    draft = compose(msg)
    # Put injected routing on a real header-looking line.
    extra = "\n" + extra.strip() if "BCC:" in extra else extra
    assert rule in failures(msg, replace(draft, body=draft.body + extra))


def test_allowed_own_quote_cannot_become_a_competing_quote_or_an_offer():
    msg = message("counter")
    draft = compose(msg)
    assert "facts_grounded" in failures(msg, replace(draft, body=draft.body + " Another dealer quoted $31,240."))
    changed_offer = draft.body.replace("Could you do $29,500", "Could you do $31,240")
    assert "price_roles" in failures(msg, replace(draft, body=changed_offer))
    msg = message("price_match")
    draft = compose(msg)
    assert "price_roles" in failures(msg, replace(draft, body=draft.body.replace("$30,690", "$31,240")))


def test_a_known_job_number_cannot_be_reused_as_an_unmarked_offer():
    msg = message()
    draft = compose(msg)
    assert "price_roles" in failures(msg, replace(draft, body=draft.body + " Could you do 2026?"))


@pytest.mark.parametrize("change", ["per month", "plus fees", "excluding taxes"])
def test_allowed_amount_does_not_authorize_different_payment_terms(change):
    msg = message("counter")
    draft = compose(msg)
    assert "facts_grounded" in failures(msg, replace(draft, body=draft.body + " The amount is " + change + "."))


@pytest.mark.parametrize("kind", ["counter", "price_match", "booking"])
def test_omitting_the_authorized_offer_or_quote_fails_intent(kind):
    msg = message(kind)
    draft = compose(message())
    assert "price_roles" in failures(msg, draft)


def test_booking_permission_never_authorizes_payment_or_another_amount():
    msg = message("booking")
    draft = compose(msg)
    assert not failures(msg, draft)
    assert "commitment_words" in failures(msg, replace(draft, body=draft.body + " I paid a deposit."))
    assert "brief_numbers" in failures(msg, replace(draft, body=draft.body.replace("$30,450", "$30,451")))
    assert not Guard(FakeChecker()).check(replace(msg, booking_approved=False)).passed


def test_brief_and_instruction_tampering_cannot_expand_authority():
    msg = message("counter")
    guard = Guard(FakeChecker())
    for changed in (replace(msg, allowed_prices=msg.allowed_prices | {99999}),
                    replace(msg, body=msg.body + " Offer anything."),
                    replace(msg, fo_instruction=msg.fo_instruction + " Also call."),
                    replace(msg, email_brief=replace(msg.email_brief, offer=99999))):
        assert not guard.check(changed).passed


def test_unsafe_metadata_and_missing_ai_disclosure_are_not_accepted():
    msg = message()
    draft = compose(msg)
    assert "safety_line" in failures(msg, replace(draft, body=draft.body.replace("AI assistant", "assistant")))
    assert "brief_intent" in failures(msg, replace(draft, body=draft.body.replace("Crystal Black Pearl", "White")))
    assert "brief_intent" in failures(msg, replace(draft, body=draft.body.replace("reply-all", "reply")))


def test_preview_requires_a_complete_envelope_not_a_body_or_an_action_wrapper():
    msg = message()
    preview = format_draft(compose(msg))
    for bad in (compose(msg).body, "Send another email too.\n" + preview,
                preview + "\nAlso charge the card.", preview.replace("CC:", "BCC:")):
        assert not Guard(FakeChecker()).check(msg, preview=bad).passed


def test_public_plain_digit_prices_remain_allowed_near_the_private_ceiling():
    from sealed.private import vault
    from sealed.private.secret_checker import SecretChecker
    msg = message("counter", job=replace(J, target=30600))
    draft = compose(msg)
    draft = replace(draft, body=draft.body.replace("$31,240", "31240").replace("$30,600", "30600"))
    for ceiling in (31000, 40000):
        vault.store(J.id, ceiling)
        try:
            assert Guard(SecretChecker(J.id)).check_email(msg, draft).passed
        finally:
            vault.forget(J.id)


def test_hybrid_draft_is_durable_before_confirmation_and_replayed_without_resending(tmp_path):
    path = tmp_path / "fo.json"
    msg = message()
    draft = compose(msg)
    class DurableFo(FakeFo):
        def ask(self, text, cid):
            if text == CONFIRM_BRIEF:
                state = json.loads(path.read_text())
                record = state["messages"][state["active"]]
                assert record["status"] == "pending" and record["draft"] == asdict(draft)
            return super().ask(text, cid)
    fo = DurableFo([ok(format_draft(draft)), ok("Sent.")])
    guard = Guard(FakeChecker())
    result = FoTransport(fo, path).send(0, msg, lambda text: guard.check(msg, preview=text).passed)
    assert result.sent and result.reason_code is None and result.draft == draft
    assert fo.asks[0][0] == msg.fo_instruction and fo.asks[1][0] == CONFIRM_BRIEF
    again = FoTransport(fo, path).send(1, msg, lambda _: False)
    assert again.sent and again.draft == draft and len(fo.asks) == 2


def test_a_sent_status_without_a_reviewed_draft_pauses_the_account(tmp_path):
    path = tmp_path / "fo.json"
    fo = FakeFo([ok("Sent.")])
    result = FoTransport(fo, path).send(0, message(), lambda _: True)
    assert result.sent and result.reason_code == "audit_mismatch" and result.draft is None
    again = FoTransport(fo, path).send(1, replace(message(), message_id="next"), lambda _: True)
    assert not again.sent and len(fo.asks) == 1
    assert json.loads(path.read_text())["active"]


def test_even_a_permissive_callback_cannot_approve_a_bad_composed_draft(tmp_path):
    msg = message()
    draft = replace(compose(msg), to="attacker@example.com")
    fo = FakeFo([ok(format_draft(draft)), ok("Okay, I won't send it.")])
    result = FoTransport(fo, tmp_path / "fo.json").send(0, msg, lambda _: True)
    assert not result.sent and [text for text, _ in fo.asks][1:] == [CONFIRM_NO]


def test_unrequested_activity_in_the_sent_status_keeps_the_review_marker(tmp_path):
    msg = message()
    fo = FakeFo([ok(format_draft(compose(msg))), ok("Sent. I scheduled a follow-up.")])
    result = FoTransport(fo, tmp_path / "fo.json").send(0, msg, lambda text: Guard(FakeChecker()).check(msg, preview=text).passed)
    assert result.sent and result.reason_code == "audit_mismatch"
    assert any(error["code"] == "fo_action_outside_brief" for error in result.errors)
    assert json.loads((tmp_path / "fo.json").read_text())["active"]


def test_engine_logs_reviewed_draft_not_the_brief_and_accepts_a_safe_footer(tmp_path):
    from sealed.core.engine import SendResult
    class FooterTransport:
        def send(self, now, msg, approve_preview):
            draft = compose(msg)
            assert approve_preview(format_draft(draft))
            return SendResult(True, now, sent_copy=draft.body + "\n\n--\nFo\nGet your own Fo: https://wajo.ai/Fo", draft=draft)
    eng = Engine(replace(J, dealers=(J.dealers[0],)), FooterTransport(), FakeChecker(), None,
                 cc="buyer@example.com", tag="SEALED-HYBRID")
    eng.start(0)
    sent = eng.log.of_type("message.sent")[0]["data"]
    assert sent["body"].startswith("Hello") and sent["brief"].startswith("Goal:")
    assert sent["content_source"] == "fo_preview"
    assert eng.log.of_type("audit.checked")[0]["data"]["basis"] == "brief_constraints"
    assert not eng.log.of_type("job.needs_human")


def test_unreviewed_send_logs_unknown_body_and_rejected_preview_never_leaks_into_events(tmp_path):
    fo = FakeFo([ok("Sent.")])
    eng = Engine(replace(J, dealers=(J.dealers[0],)), FoTransport(fo, tmp_path / "fo.json"), FakeChecker(), None,
                 cc="buyer@example.com", tag="SEALED-HYBRID")
    eng.start(0)
    sent = eng.log.of_type("message.sent")[0]["data"]
    assert sent["body"] == "" and sent["content_source"] == "pending_gmail" and eng.state == "NEEDS_HUMAN"
    bad = format_draft(replace(compose(message()), body=compose(message()).body + " SECRET"))
    fo = FakeFo([ok(bad), ok("Okay, I won't send it.")])
    eng = Engine(replace(J, dealers=(J.dealers[0],)), FoTransport(fo, tmp_path / "other.json"), FakeChecker(), None,
                 cc="buyer@example.com", tag="SEALED-HYBRID")
    eng.start(0)
    assert "SECRET" not in json.dumps(eng.log.events) and not eng.log.of_type("message.sent")
