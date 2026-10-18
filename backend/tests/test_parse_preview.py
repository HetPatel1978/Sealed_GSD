"""Gmail previews select current authenticated evidence and never send/consume."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sealed.ingest.mail import Binding
from sealed.ingest.parse import prepare_inputs, preview
from sealed.ingest.store import InboxStore
from sealed.parser.quote import ParserError, validate
from sealed.parser.store import ParseStore
from tests.test_gmail_ingestion import decoded, wire
from tests.test_quote_parser import SPEC, TEXT, extraction

BINDING = Binding("job", "run", "seller", "seller@example.org", "[SEALED-test_run]",
    "2026-10-18T12:00:00+00:00", "rfq", "<original@fo.example>", "555")
NOW = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)


class Reader:
    address = "buyer@example.com"

    def __init__(self, mail):
        self.mail = {m.uid: m for m in mail}

    def search(self, query):
        return list(self.mail)

    def fetch(self, uid):
        return self.mail[uid]


class Extractor:
    model = "test-model"

    def __init__(self):
        self.calls = []

    def parse(self, text, context):
        self.calls.append(text)
        return validate(extraction(), text, context)


def mail(text=TEXT, **changes):
    return replace(decoded(wire(text=text), received_at="2026-10-18T16:00:00+00:00"), **changes)


def run(reader, extractor, inbox, cache, **kwargs):
    return preview(reader, [BINDING], inbox, cache, extractor,
        specification=SPEC, currency="USD", now=NOW, **kwargs)


def test_repeated_preview_caches_model_result_and_leaves_inbox_pending_after_restart(tmp_path):
    extractor = Extractor()
    reader = Reader([mail()])
    for iteration in range(2):
        with InboxStore(tmp_path / "inbox.sqlite3") as inbox, ParseStore(tmp_path / "parses.sqlite3") as cache:
            report = run(reader, extractor, inbox, cache)
            item = report["replies"][0]
            assert item["ledger_firm"] and item["parsed"]["price_total"] == 95
            assert item["cached"] is bool(iteration)
            assert len(inbox.pending(reader.address)) == 1
            assert not report["engine_delivery"] and not report["inbox_acknowledged"] and report["messages_sent"] == 0
    assert extractor.calls == [TEXT]


def test_latest_revision_is_parsed_instead_of_replaying_an_older_quote(tmp_path):
    reader = Reader([mail(text="Old written total $100 USD.", uid="1", gmail_id="old", received_at="2026-10-18T13:00:00+00:00"),
        mail(uid="2", gmail_id="new")])
    extractor = Extractor()
    with InboxStore(tmp_path / "inbox.sqlite3") as inbox, ParseStore(tmp_path / "parses.sqlite3") as cache:
        report = run(reader, extractor, inbox, cache)
        assert report["replies"][0]["evidence_ref"] == "gmail:new"
        assert len(inbox.pending(reader.address)) == 2
    assert extractor.calls == [TEXT]


def test_newer_unverified_reply_prevents_fallback_to_older_ready_quote(tmp_path):
    reader = Reader([mail(uid="1", gmail_id="old"), mail(uid="2", gmail_id="new", authenticated=False,
        flags=("sender_auth_unverified",), received_at="2026-10-18T16:30:00+00:00")])
    extractor = Extractor()
    with InboxStore(tmp_path / "inbox.sqlite3") as inbox, ParseStore(tmp_path / "parses.sqlite3") as cache:
        report = run(reader, extractor, inbox, cache)
        assert report["replies"][0]["reason"] == "latest_reply_needs_identity_review"
        assert report["replies"][0]["evidence_ref"] == "gmail:new"
        assert len(inbox.pending(reader.address, status="review")) == 1
    assert extractor.calls == []


def test_model_failure_keeps_reply_pending_and_does_not_cache_a_result(tmp_path):
    class Failed(Extractor):
        def parse(self, text, context):
            raise ParserError("parser_timeout")
    with InboxStore(tmp_path / "inbox.sqlite3") as inbox, ParseStore(tmp_path / "parses.sqlite3") as cache:
        with pytest.raises(ParserError, match="parser_timeout"):
            run(Reader([mail()]), Failed(), inbox, cache)
        assert len(inbox.pending("buyer@example.com")) == 1
        assert cache.db.execute("SELECT COUNT(*) FROM quote_parses").fetchone()[0] == 0


def test_preview_does_not_apply_one_specification_to_multiple_jobs(tmp_path):
    extractor = Extractor()
    with InboxStore(tmp_path / "inbox.sqlite3") as inbox, ParseStore(tmp_path / "parses.sqlite3") as cache:
        with pytest.raises(ParserError, match="one_job_run_required"):
            preview(Reader([mail()]), [BINDING, replace(BINDING, job_id="other")], inbox, cache, extractor,
                specification=SPEC, currency="USD", now=NOW)
    assert not extractor.calls


def test_prepare_only_saves_exact_public_input_without_an_extractor_or_consumption(tmp_path):
    with InboxStore(tmp_path / "inbox.sqlite3") as inbox:
        result = prepare_inputs(Reader([mail()]), [BINDING], inbox, specification=SPEC, currency="USD", now=NOW)
        assert result["model_requests"] == 0 and result["messages_sent"] == 0
        assert not result["inbox_acknowledged"] and len(inbox.pending("buyer@example.com")) == 1
        model_input = result["inputs"][0]["model_input"]
        assert model_input["seller_reply"] == TEXT
        assert set(model_input) == {"requested_specification", "expected_currency", "received_at", "seller_reply"}


def test_scoped_live_test_stops_if_prepared_email_changes_before_extraction(tmp_path):
    extractor = Extractor()
    with InboxStore(tmp_path / "inbox.sqlite3") as inbox, ParseStore(tmp_path / "parses.sqlite3") as cache:
        prepared = prepare_inputs(Reader([mail()]), [BINDING], inbox, specification=SPEC, currency="USD", now=NOW)
        reader = Reader([mail(text=TEXT + " Changed private text.", gmail_id="new")])
        with pytest.raises(ParserError, match="prepared_input_changed"):
            run(reader, extractor, inbox, cache, approved_input=prepared)
        assert not extractor.calls
        assert run(Reader([mail()]), extractor, inbox, cache, approved_input=prepared)["replies"][0]["ledger_firm"]


def test_invalid_prepared_report_does_not_call_extractor(tmp_path):
    extractor = Extractor()
    with InboxStore(tmp_path / "inbox.sqlite3") as inbox, ParseStore(tmp_path / "parses.sqlite3") as cache:
        with pytest.raises(ParserError, match="invalid_prepared_input"):
            run(Reader([mail()]), extractor, inbox, cache, approved_input=[])
    assert not extractor.calls
