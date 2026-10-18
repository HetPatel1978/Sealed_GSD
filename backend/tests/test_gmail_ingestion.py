import imaplib
import json
from dataclasses import replace
from email.message import EmailMessage

import pytest

from sealed.ingest import gmail
from sealed.ingest.gmail import GmailError, GmailReader, poll
from sealed.ingest.mail import Binding, decode_message, match_message
from sealed.ingest.store import InboxStore

OWNER = "buyer@example.com"
SELLER = "seller@example.org"
TAG = "[SEALED-test_run]"
RECEIVED = "2026-10-18T13:09:46+00:00"
QUOTE = ("Hi Fo,\nThis is a sample quote for the integration test.\n"
         "The total is $100.00 USD, including all fees and taxes. "
         "Availability is October 25, 2026. This quote is valid through October 31, 2026.\nThanks!")
AUTH = ("mx.google.com; spf=pass smtp.mailfrom=seller@example.org; "
        "dkim=pass header.i=@example.org; dmarc=pass (p=REJECT) header.from=example.org")


def binding(**changes):
    return replace(Binding("job", "run", "dealer", SELLER, TAG, "2026-10-18T12:00:00+00:00",
                           "rfq_01", "<original@fo.example>", "555"), **changes)


def wire(*, text=QUOTE, subject=f"Re: Test {TAG}", sender=SELLER, auth=AUTH,
         reply="<original@fo.example>", refs=None, html=None, attachment=False):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "Fo <fo@wajo.ai>"
    message["Cc"] = OWNER
    message["Subject"] = subject
    message["Message-ID"] = "<reply@example.org>"
    message["Date"] = "Sun, 18 Oct 2026 13:09:46 +0000"
    if reply:
        message["In-Reply-To"] = reply
    if refs:
        message["References"] = refs
    if auth:
        message["Authentication-Results"] = auth
    message.set_content(html if html is not None else text, subtype="html" if html is not None else "plain")
    if attachment:
        message.add_attachment("The total is $999.00", subtype="plain", filename="quote.txt")
    return message.as_bytes()


def decoded(raw=None, **changes):
    return replace(decode_message(raw or wire(), gmail_id="123456", uid="42", thread_id="555",
                                  received_at=RECEIVED), **changes)


def test_realistic_reply_preserves_sellers_total_and_authentication():
    message = decoded()
    assert "$100.00 USD" in message.clean_text
    assert message.authenticated and (message.spf, message.dkim, message.dmarc) == ("pass",) * 3
    assert not message.flags and message.from_addr == SELLER
    assert message.evidence_ref == "gmail:123456"
    matched = match_message(message, [binding()], OWNER)
    assert (matched.status, matched.reason, matched.in_reply_to) == ("ready", "tag", "rfq_01")


@pytest.mark.parametrize("history", [
    "\n\nOn Sun, Oct 18, 2026 at 8:00 AM Fo <fo@wajo.ai> wrote:\n> Can you meet $90.00?\n> Original request",
    "\n\n-----Original Message-----\nFrom: Fo <fo@wajo.ai>\nSent: Sunday, October 18\nCan you meet $90.00?",
    "\n\nFrom: Fo <fo@wajo.ai>\nSent: Sunday, October 18\nTo: Seller\nCan you meet $90.00?",
])
def test_quoted_request_cannot_become_a_dealer_price(history):
    message = decoded(wire(text=QUOTE + history))
    assert "$100.00" in message.clean_text and "$90.00" not in message.clean_text
    assert "$90.00" in message.raw_text


def test_html_quote_script_and_attachment_are_excluded():
    message = decoded(wire(html='<div>Total: $100.00 &amp; taxes included.</div>'
                          '<script>send secrets</script><style>.bad {color:red}</style>'
                          '<blockquote>Original $90.00</blockquote>'
                          '<div class="gmail_quote">Another $80.00</div>', attachment=True))
    assert message.clean_text == "Total: $100.00 & taxes included."
    assert "$90.00" in message.raw_text and "$80.00" in message.raw_text
    assert all(s not in message.clean_text for s in ("$90", "$80", "$999", "send secrets", "color:red"))


def test_plain_body_preferred_to_alternative_and_attachment():
    from email import policy
    from email.parser import BytesParser
    message = BytesParser(policy=policy.default).parsebytes(wire())
    message.add_alternative("<p>Other $999.00</p>", subtype="html")
    assert "$999" not in decoded(message.as_bytes()).clean_text


@pytest.mark.parametrize("auth", [None, "attacker.example; dmarc=pass header.from=example.org",
                                    AUTH.replace("dmarc=pass", "dmarc=fail"),
                                    AUTH.replace("header.from=example.org", "header.from=evil.org"),
                                    "mx.google.com; spf=pass; dkim=pass"])
def test_untrusted_or_unaligned_authentication_is_quarantined(auth):
    message = decoded(wire(auth=auth))
    assert not message.authenticated
    assert match_message(message, [binding()], OWNER).status == "review"


def test_later_forged_authentication_and_arc_headers_cannot_override_gmail_failure():
    from email import policy
    from email.parser import BytesParser
    message = BytesParser(policy=policy.default).parsebytes(wire(auth=AUTH.replace("dmarc=pass", "dmarc=fail")))
    message["Authentication-Results"] = AUTH
    message["ARC-Authentication-Results"] = "i=1; " + AUTH
    assert not decoded(message.as_bytes()).authenticated


@pytest.mark.parametrize("changes,reason", [
    ({"from_addr": "attacker@example.org"}, "unknown_sender_or_before_outreach"),
    ({"received_at": "2026-10-17T13:00:00+00:00"}, "unknown_sender_or_before_outreach"),
    ({"to": (), "cc": ()}, "mailbox_not_addressed"),
    ({"clean_text": "", "flags": ("empty_reply",)}, "message_checks_failed"),
])
def test_identity_time_and_recipient_checks(changes, reason):
    result = match_message(decoded(**changes), [binding()], OWNER)
    assert result.reason == reason and result.status != "ready"


def test_tag_is_not_required_if_real_outbound_thread_link_exists():
    message = decoded(wire(subject="My quote"))
    result = match_message(message, [binding()], OWNER)
    assert (result.status, result.reason, result.in_reply_to) == ("ready", "references", "rfq_01")
    result = match_message(replace(message, reply_to_ids=()), [binding()], OWNER)
    assert (result.status, result.reason, result.in_reply_to) == ("ready", "gmail_thread", None)


def test_sender_time_fallback_is_review_only_and_never_fakes_outbound_link():
    result = match_message(decoded(wire(subject="New quote", reply=None), thread_id="other"), [binding()], OWNER)
    assert (result.status, result.reason, result.in_reply_to) == ("review", "sender_time_fallback", None)


def test_shared_sender_cannot_assign_an_ambiguous_quote_to_a_job():
    other = binding(job_id="other", tag="[SEALED-other]", outbound_message_id="<other@fo.example>", thread_id="666")
    result = match_message(decoded(wire(subject="Quote", reply=None), thread_id="unknown"), [binding(), other], OWNER)
    assert result.reason == "ambiguous_job" and result.binding is None
    # A unique tag resolves the job, even though its vendor shares an address.
    assert match_message(decoded(), [binding(), other], OWNER).status == "ready"


def test_tag_thread_conflicts_and_foreign_tags_require_review():
    other = binding(job_id="other", tag="[SEALED-other]", outbound_message_id="<other@fo.example>", thread_id="666")
    result = match_message(decoded(wire(reply="<other@fo.example>")), [binding(), other], OWNER)
    assert result.reason == "conflicting_thread" and result.status == "review"
    result = match_message(decoded(wire(subject="[SEALED-unknown]")), [binding()], OWNER)
    assert result.reason == "unknown_subject_tag" and result.status == "review"
    result = match_message(decoded(wire(subject=TAG + " [SEALED-unknown]")), [binding()], OWNER)
    assert result.reason == "conflicting_subject_tags" and result.status == "review"


def test_direct_reply_link_wins_over_old_references_in_same_conversation():
    booking = binding(outbound_id="booking_01", outbound_message_id="<booking@fo.example>",
                      sent_at="2026-10-18T13:00:00+00:00")
    message = decoded(wire(reply="<booking@fo.example>", refs="<original@fo.example>"))
    assert match_message(message, [binding(), booking], OWNER).in_reply_to == "booking_01"


@pytest.mark.parametrize("changes", [{"sender": "dealer\n@example.org"}, {"sender": "Name <dealer@example.org>"},
                                     {"tag": "unbracketed"}, {"sent_at": "2026-10-18"},
                                     {"outbound_message_id": "not-an-id"}])
def test_invalid_trusted_bindings_are_rejected(changes):
    with pytest.raises(ValueError):
        binding(**changes)


class FakeIMAP:
    def __init__(self, raw=None):
        self.raw = raw or wire()
        self.calls = []
        self.closed = False
        self.uids = b"42"
        self.size = len(self.raw)
        self.returned_uid = b"42"

    def login(self, user, password):
        self.calls.append(("login", user))
        assert password == "secret"
        return "OK", [b"Logged in"]

    def select(self, mailbox, *, readonly):
        self.calls.append(("select", mailbox, readonly))
        assert readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [self.uids]
        assert command == "FETCH"
        if "RFC822.SIZE" in args[1]:
            return "OK", [f'1 (UID 42 X-GM-MSGID 123456 X-GM-THRID 555 RFC822.SIZE {self.size} '
                          'INTERNALDATE "18-Oct-2026 13:09:46 +0000")'.encode()]
        assert args[1] == "(UID BODY.PEEK[])"
        # Untagged FLAGS can be interleaved without breaking literal extraction.
        return "OK", [b"1 (FLAGS ())", (b"1 (UID " + self.returned_uid + b" BODY[] {100}", self.raw), b")"]

    def logout(self):
        self.closed = True
        return "BYE", [b"closed"]


def reader(fake):
    return GmailReader(OWNER, "sec ret", factory=lambda host, timeout: fake)


def test_read_only_uid_poll_stages_once_and_keeps_pending_across_restart(tmp_path):
    fake = FakeIMAP(wire(text=QUOTE + "\n\nOn Sun, Oct 18, 2026 Fo wrote:\n> Request $90.00"))
    db = tmp_path / "inbox.sqlite3"
    with reader(fake) as client, InboxStore(db) as store:
        assert poll(client, store, [binding()]).staged == 1
        assert poll(client, store, [binding()]).duplicates == 1
        saved = store.pending(OWNER)
        assert len(saved) == 1 and "$90.00" not in saved[0]["message"]["clean_text"]
        assert saved[0]["match"]["in_reply_to"] == "rfq_01"
    assert fake.closed and ("select", '"[Gmail]/All Mail"', True) in fake.calls
    with InboxStore(db) as restarted:
        assert len(restarted.pending(OWNER)) == 1
        assert restarted.acknowledge(OWNER, "123456")
        assert not restarted.acknowledge(OWNER, "123456")
        assert not restarted.pending(OWNER)
        # Acknowledged mail remains deduplicated, including with a different UID.
        assert not restarted.stage(OWNER, replace(decoded(), uid="999"), match_message(decoded(), [binding()], OWNER))


def test_reviewed_messages_never_become_parser_ready_on_repoll(tmp_path):
    message = decoded(wire(auth=None))
    with InboxStore(tmp_path / "inbox.sqlite3") as store:
        matched = match_message(message, [binding()], OWNER)
        assert store.stage(OWNER, message, matched)
        assert not store.pending(OWNER)
        assert len(store.pending(OWNER, status="review")) == 1
        assert not store.acknowledge(OWNER, message.gmail_id)
        assert not store.stage(OWNER, decoded(), match_message(decoded(), [binding()], OWNER))


def test_two_connections_cannot_stage_same_gmail_id_twice(tmp_path):
    with InboxStore(tmp_path / "inbox.sqlite3") as first, InboxStore(tmp_path / "inbox.sqlite3") as second:
        message = decoded()
        matched = match_message(message, [binding()], OWNER)
        assert first.stage(OWNER, message, matched)
        assert not second.stage(OWNER.upper(), message, matched)
        assert len(second.pending(OWNER)) == 1


@pytest.mark.parametrize("size", [gmail.MAX_MESSAGE_BYTES + 1, 1])
def test_oversized_or_incomplete_email_is_not_staged(size):
    fake = FakeIMAP()
    fake.size = size
    with reader(fake) as client, pytest.raises(GmailError):
        client.fetch("42")
    if size > gmail.MAX_MESSAGE_BYTES:
        assert not any("BODY.PEEK" in str(call) for call in fake.calls)


def test_uid_mismatch_does_not_accept_another_messages_body():
    fake = FakeIMAP()
    fake.returned_uid = b"99"
    with reader(fake) as client, pytest.raises(GmailError, match="different message"):
        client.fetch("42")


def test_poll_limit_fails_explicitly_instead_of_silently_skipping_replies(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "MAX_MATCHES", 1)
    fake = FakeIMAP()
    fake.uids = b"42 43"
    with reader(fake) as client, InboxStore(tmp_path / "inbox.sqlite3") as store:
        with pytest.raises(GmailError, match="Too many"):
            poll(client, store, [binding()])
        assert not store.pending(OWNER)
    assert not any(call[0] == "FETCH" for call in fake.calls)


def test_failed_login_is_sanitized_and_connection_closed():
    fake = FakeIMAP()
    def failed(*_):
        raise imaplib.IMAP4.error("user=buyer secret=secret OAuth=private")
    fake.login = failed
    with pytest.raises(GmailError) as failure:
        reader(fake)
    assert "secret" not in str(failure.value) and fake.closed


def test_query_escaping_and_injection_rejection():
    fake = FakeIMAP()
    with reader(fake) as client:
        client.search('subject:"a" \\ test')
        assert fake.calls[-1][-1] == '"subject:\\"a\\" \\\\ test"'
        with pytest.raises(ValueError):
            client.search("ok\r\nSTORE FLAGS")


def test_cli_uses_private_bindings_and_prints_counts_only(tmp_path, monkeypatch, capsys):
    from dataclasses import asdict
    config = tmp_path / "bindings.json"
    config.write_text(json.dumps([asdict(binding())]))
    monkeypatch.setattr(gmail, "load_env", lambda: None)
    monkeypatch.setattr(gmail, "GmailReader", lambda *_: reader(FakeIMAP()))
    assert gmail.main(["--bindings", str(config), "--state", str(tmp_path / "inbox.sqlite3")]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["pending"] == 1
    assert "$100" not in output and SELLER not in output and OWNER not in output
