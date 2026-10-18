import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from sealed.core.engine import Engine
from sealed.core.guard import Guard
from sealed.fo.adapter import FoTransport
from sealed.fo.smoke import build_message as legacy_message
from sealed.ingest.audit import AuditRequest, AuditStore, GmailAuditor, audit_copies, reply_binding
from sealed.ingest.mail import decode_message
from sealed.sim.email import compose
from tests.helpers import FakeChecker, job
from tests.test_email_briefs import message
from tests.test_fo_adapter import FakeFo, ok
from sealed.core.email_policy import fo_signature_link, format_draft

OWNER = "buyer@example.com"
START = "2026-10-18T12:00:00+00:00"
RECEIVED = "2026-10-18T12:01:00+00:00"
NOW = datetime.fromisoformat("2026-10-18T12:02:00+00:00")
AUTH = "mx.google.com; spf=pass; dkim=pass; dmarc=pass header.from=wajo.ai"


def request(msg=None, **changes):
    return replace(AuditRequest(msg or message(), START, ("fo@wajo.ai", OWNER)), **changes)


def copy(msg=None, *, body=None, sender="Fo <fo@wajo.ai>", auth=AUTH, **changes):
    msg = msg or message()
    mail = EmailMessage()
    for name, value in (("From", sender), ("To", msg.to), ("Cc", msg.cc), ("Subject", msg.subject),
                        ("Message-ID", "<copy@wajo.ai>")):
        mail[name] = value
    if auth:
        mail["Authentication-Results"] = auth
    mail.set_content(body if body is not None else compose(msg).body)
    decoded = decode_message(mail.as_bytes(), gmail_id="123456", uid="42", thread_id="555", received_at=RECEIVED)
    return replace(decoded, **changes)


def audit(req=None, copies=None, **kwargs):
    return audit_copies(req or request(), copies if copies is not None else [copy()], OWNER, Guard(FakeChecker()), now=kwargs.pop("now", NOW), **kwargs)


def test_composed_copy_is_audited_with_real_headers_auth_and_safe_signature():
    mail = copy(body=compose(message()).body + "\n\n--\nFo\nGet your own Fo: https://wajo.ai/Fo")
    result = audit(copies=[mail])
    assert result.matched and result.guard_passed and result.basis == "brief_constraints"
    assert result.evidence.authentication_source == "gmail_dmarc"
    assert result.candidate_refs == ("gmail:123456",)
    bound = reply_binding(request(), result)
    assert (bound.outbound_message_id, bound.thread_id, bound.outbound_id) == (mail.message_id, "555", "m_01")


@pytest.mark.parametrize("changes,rule", [
    ({"to": ("other@example.org",)}, "recipient_exact"),
    ({"to": ("a@x.example", "other@example.org")}, "recipient_exact"),
    ({"cc": ()}, "cc_exact"),
    ({"cc": (OWNER, "extra@example.org")}, "cc_exact"),
    ({"bcc": ("hidden@example.org",)}, "bcc_absent"),
    ({"reply_to": ("attacker@example.org",)}, "reply_to_expected"),
    ({"subject": "Changed subject [SEALED-HYBRID]"}, "subject_exact"),
    ({"authenticated": False, "flags": ("sender_auth_unverified",)}, "sender_authenticated"),
    ({"flags": ("duplicate_routing_header",)}, "message_well_formed"),
])
def test_actual_routing_authentication_and_header_failures_require_review(changes, rule):
    result = audit(copies=[replace(copy(), **changes)])
    assert result.state == "review" and not result.matched
    assert not next(check for check in result.rules if check["rule"] == rule)["passed"]
    with pytest.raises(ValueError):
        reply_binding(request(), result)


def test_pinned_message_still_checks_changed_sender_tag_and_time():
    req = request(sent_message_id="<copy@wajo.ai>")
    result = audit(req, [replace(copy(), from_addr="attacker@example.org", subject="No tag",
                                 received_at="2026-10-17T12:00:00+00:00")])
    failed = {r["rule"] for r in result.rules if not r["passed"]}
    assert {"sender_expected", "subject_exact", "send_window"} <= failed
    assert result.state == "review"


def test_known_thread_finds_copy_with_lost_tag_but_does_not_approve_it():
    result = audit(request(thread_id="555"), [replace(copy(), subject="No tag")])
    assert result.cc_copy_found and result.state == "review"


def test_missing_copy_is_pending_then_requires_review_after_the_send_window():
    assert audit(copies=[]).state == "pending"
    result = audit(copies=[], now=datetime.fromisoformat("2026-10-18T13:00:00+00:00"))
    assert result.state == "review" and result.reason_code == "audit_missing" and not result.cc_copy_found


def test_vendor_reply_cannot_be_mistaken_for_fo_sent_evidence():
    mail = copy(sender="Seller <a@x.example>")
    assert audit(copies=[mail]).state == "pending"


def test_multiple_possible_outbounds_are_not_guessed_and_duplicate_uid_reads_are_deduped():
    assert audit(copies=[copy(), copy()]).matched
    result = audit(copies=[copy(), copy(gmail_id="654321", message_id="<second@wajo.ai>")])
    assert result.reason_code == "audit_ambiguous" and result.evidence is None
    assert len(result.candidate_refs) == 2


def test_own_gmail_sent_metadata_can_authenticate_own_outgoing_mail_only():
    sent = copy(sender=OWNER, auth=None, gmail_labels=("\\Sent",))
    result = audit(copies=[sent])
    assert result.matched and result.evidence.authentication_source == "gmail_sent_label"
    assert not audit(copies=[replace(sent, gmail_labels=())]).matched
    assert not audit(copies=[replace(sent, from_addr="fo@wajo.ai")]).matched


def test_actual_body_is_checked_including_a_footer_that_reply_stripping_would_hide():
    msg = message()
    mail = copy(body=compose(msg).body + "\n\n--\nSECRET\nWe accept and paid a deposit of $99.")
    assert "SECRET" not in mail.clean_text and "SECRET" in mail.raw_text
    result = audit(copies=[mail])
    assert result.state == "review"
    failed = {r["rule"] for r in result.rules if not r["passed"]}
    assert {"secret_checker", "brief_numbers", "commitment_words"} <= failed
    # The report contains booleans and evidence identifiers, never unsafe excerpts.
    assert "SECRET" not in json.dumps(asdict(result)) and "$99" not in json.dumps(asdict(result))


def test_actual_display_names_are_scanned_even_when_mailbox_addresses_match():
    result = audit(copies=[copy(sender="SECRET <fo@wajo.ai>")])
    assert not next(r for r in result.rules if r["rule"] == "secret_checker")["passed"]


@pytest.mark.parametrize("variant", ["unsafe_html", "unknown_link", "attachment"])
def test_safe_plain_text_cannot_hide_an_unsafe_html_alternative_or_attachment(variant):
    from html import escape
    msg = message()
    mail = EmailMessage()
    for key, value in (("From", "fo@wajo.ai"), ("To", msg.to), ("Cc", msg.cc),
                       ("Subject", msg.subject), ("Message-ID", "<copy@wajo.ai>"), ("Authentication-Results", AUTH)):
        mail[key] = value
    mail.set_content(compose(msg).body)
    html = "<div>" + escape(compose(msg).body).replace("\n", "<br>") + "</div>"
    if variant == "unsafe_html":
        html += "<p>SECRET. We accept and paid $99.</p>"
    elif variant == "unknown_link":
        html += '<a href="https://attacker.example.org/pay">Fo</a>'
    else:
        mail.add_attachment(b"unrequested document", maintype="application", subtype="pdf", filename="terms.pdf")
    if variant != "attachment":
        mail.add_alternative(html, subtype="html")
    decoded = decode_message(mail.as_bytes(), gmail_id="123456", uid="42", thread_id="555", received_at=RECEIVED)
    result = audit(copies=[decoded])
    assert not result.matched
    assert "SECRET" not in json.dumps(asdict(result))


def test_an_ordinary_html_version_with_known_signature_links_is_allowed():
    from html import escape
    msg = message()
    mail = EmailMessage()
    for key, value in (("From", "fo@wajo.ai"), ("To", msg.to), ("Cc", msg.cc),
                       ("Subject", msg.subject), ("Message-ID", "<copy@wajo.ai>"), ("Authentication-Results", AUTH)):
        mail[key] = value
    mail.set_content(compose(msg).body)
    mail.add_alternative("<div>" + escape(compose(msg).body).replace("\n", "<br>")
                         + '</div><a href="https://wajo.ai/Fo">Get your own Fo</a>', subtype="html")
    decoded = decode_message(mail.as_bytes(), gmail_id="123456", uid="42", thread_id="555", received_at=RECEIVED)
    assert audit(copies=[decoded]).matched


@pytest.mark.parametrize("url,allowed", [
    ("https://www.instagram.com/wajo_ai/", True),
    ("https://www.linkedin.com/company/wajo-ai/", True),
    ("https://media.wajo.ai/uploads/wajo-logo-light-64-de1291cd.png", True),
    ("https://media.wajo.ai/uploads/unknown.png", False),
    ("https://wajo.ai/Fo?secret=PRIVATE", False),
    ("https://wajo.ai.attacker.example/Fo", False),
    ("https://wajo.ai@attacker.example/Fo", False),
])
def test_signature_allowlist_normalizes_verified_urls_without_expanding_authority(url, allowed):
    assert fo_signature_link(url, images=True) == allowed


def test_legacy_signature_difference_stays_flagged_under_the_original_contract():
    msg = legacy_message("a@x.example", OWNER, "smoke_01")
    mail = copy(msg, body=msg.body + "\n\n--\nFo\nGet your own Fo: https://wajo.ai/Fo")
    result = audit(request(msg), [mail])
    assert result.basis == "exact_payload" and result.state == "review"
    assert not next(r for r in result.rules if r["rule"] == "body_exact")["passed"]
    assert audit(request(msg), [copy(msg)]).matched


def test_durable_audit_observations_do_not_duplicate_on_restart_or_cross_accounts(tmp_path):
    path = tmp_path / "audits.sqlite3"
    result = audit()
    with AuditStore(path) as store:
        assert store.append(OWNER, result)
        assert not store.append(OWNER.upper(), result)
    with AuditStore(path) as store:
        assert len(store.records(OWNER)) == 1
        assert store.append("another@example.com", result)
        assert len(store.records(OWNER)) == 1


def test_reader_fetches_altered_copy_instead_of_filtering_out_bad_routing(tmp_path):
    class Reader:
        address = OWNER
        def __init__(self):
            self.queries = []
        def search(self, query):
            self.queries.append(query)
            return ["42"]
        def fetch(self, uid):
            return copy(to=("other@example.org",))
    reader = Reader()
    with AuditStore(tmp_path / "audits.sqlite3") as store:
        result = GmailAuditor(reader, Guard(FakeChecker()), store, clock=lambda: NOW).audit(request())
        assert result.state == "review"
        assert all("to:" not in q and "subject:" not in q for q in reader.queries)
        assert len(store.records(OWNER)) == 1


def test_audit_callback_runs_under_a_durable_sent_reservation_and_replays_safely(tmp_path):
    msg = message()
    path = tmp_path / "fo.json"
    called = []
    def check(outbound, attempted_at):
        state = json.loads(path.read_text())
        record = state["messages"][state["active"]]
        assert record["status"] == "sent" and record["audit_pending"]
        assert record["attempted_at"] == attempted_at
        called.append(outbound.message_id)
        return audit()
    fo = FakeFo([ok(format_draft(compose(msg))), ok("Sent.")])
    guard = Guard(FakeChecker())
    result = FoTransport(fo, path, sent_auditor=check).send(0, msg, lambda text: guard.check(msg, preview=text).passed)
    assert result.sent and result.reason_code is None and result.audit.matched
    assert json.loads(path.read_text())["active"] is None
    again = FoTransport(fo, path, sent_auditor=check).send(1, msg, lambda _: False)
    assert again.audit == result.audit and len(fo.asks) == 2 and called == [msg.message_id]


@pytest.mark.parametrize("outcome", ["pending", "review", "failure", "wrong_job"])
def test_nonpassing_audits_pause_the_account_without_losing_sent_status_or_resending(tmp_path, outcome):
    msg = message()
    path = tmp_path / "fo.json"
    def check(*_):
        if outcome == "failure":
            raise RuntimeError("Authorization: Bearer PRIVATE_TOKEN")
        if outcome == "pending":
            return audit(copies=[])
        if outcome == "wrong_job":
            return replace(audit(), job_id="other")
        return audit(copies=[copy(cc=())])
    fo = FakeFo([ok(format_draft(compose(msg))), ok("Sent.")])
    result = FoTransport(fo, path, sent_auditor=check).send(0, msg, lambda _: True)
    assert result.sent and result.reason_code is not None
    state = json.loads(path.read_text())
    assert state["active"] and state["messages"][state["active"]]["status"] == "sent"
    assert "PRIVATE_TOKEN" not in repr(result) + path.read_text()
    assert not FoTransport(fo, path).send(1, replace(msg, message_id="next"), lambda _: True).sent
    assert len(fo.asks) == 2


def test_a_passed_copy_does_not_clear_an_unrelated_fo_activity_warning(tmp_path):
    msg = message()
    path = tmp_path / "fo.json"
    fo = FakeFo([ok(format_draft(compose(msg))), ok("Sent. I scheduled a follow-up.")])
    result = FoTransport(fo, path, sent_auditor=lambda *_: audit()).send(0, msg, lambda _: True)
    assert result.audit.matched and result.reason_code == "audit_mismatch"
    assert json.loads(path.read_text())["active"]


def test_engine_applies_full_gmail_evidence_once_and_keeps_unsafe_body_out_of_events(tmp_path):
    job_one = replace(job(), dealers=(job().dealers[0],))
    # Engine-generated RFQ has this same identity and payload.
    from sealed.core.models import Decision
    from sealed.fo.messages import build
    msg = build(job_one, Decision("a", "rfq", "rfq"), "m_01", "SEALED-HYBRID", OWNER)
    result = audit(request(msg), [copy(msg)])
    fo = FakeFo([ok(format_draft(compose(msg))), ok("Sent.")])
    eng = Engine(job_one, FoTransport(fo, tmp_path / "fo.json", sent_auditor=lambda *_: result),
                 FakeChecker(), None, cc=OWNER, tag="SEALED-HYBRID")
    eng.start(0)
    eng.on_email_audit(2, result)
    assert len(eng.log.of_type("audit.checked")) == 1 and eng.trust["messages_checked_after"] == 1
    assert eng.log.of_type("audit.checked")[0]["data"]["evidence"]["ref"] == "gmail:123456"
    with pytest.raises(ValueError):
        eng.on_email_audit(3, replace(result, run_id="other"))
    bad = audit(request(msg), [copy(msg, body=compose(msg).body + "\nSECRET")])
    eng.on_email_audit(4, bad)
    assert eng.state == "NEEDS_HUMAN" and "SECRET" not in json.dumps(eng.log.events)
