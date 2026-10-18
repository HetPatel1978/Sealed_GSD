import json
from datetime import datetime, timezone

from sealed.core.email_policy import format_draft
from sealed.fo import brief_smoke
from sealed.sim.email import compose
from tests.test_fo_adapter import FakeFo, ok
from tests.test_sent_audit import copy


def args(tmp_path):
    return ["--to", "a@x.example", "--cc", "buyer@example.com", "--state", str(tmp_path / "fo.json"),
            "--audits", str(tmp_path / "audits.sqlite3"), "--binding", str(tmp_path / "binding.json"), "--audit-seconds", "0"]


def test_dry_run_has_no_network_clients_or_state_changes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(brief_smoke, "load_env", lambda: None)
    def forbidden(*_):
        raise AssertionError("Dry run created a network client")
    monkeypatch.setattr(brief_smoke, "GmailReader", forbidden)
    assert brief_smoke.main(args(tmp_path)) == 0
    output = capsys.readouterr().out
    assert "Dry run" in output and "controlled Sealed integration test" in output
    assert not list(tmp_path.iterdir())


def test_existing_account_pause_is_preserved_without_connecting_or_sending(tmp_path, monkeypatch):
    monkeypatch.setattr(brief_smoke, "load_env", lambda: None)
    path = tmp_path / "fo.json"
    original = json.dumps({"version": 2, "messages": {}, "conversations": {}, "active": "previous-review"})
    path.write_text(original)
    monkeypatch.setattr(brief_smoke, "GmailReader", lambda *_: (_ for _ in ()).throw(AssertionError("Connected")))
    assert brief_smoke.main(args(tmp_path) + ["--yes-send"]) == 1
    assert path.read_text() == original


def test_explicit_send_checks_gmail_first_audits_the_copy_and_never_resends(tmp_path, monkeypatch):
    import sealed.fo.client as client
    monkeypatch.setattr(brief_smoke, "load_env", lambda: None)
    msg = brief_smoke.build_message("a@x.example", "buyer@example.com", "brief_01")
    connected = []
    class Reader:
        address = "buyer@example.com"
        def __init__(self, *_):
            connected.append(True)
        def search(self, query):
            return ["42"]
        def fetch(self, uid):
            return copy(msg, received_at=datetime.now(timezone.utc).isoformat())
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    fo = FakeFo([ok(format_draft(compose(msg))), ok("Sent.")])
    fo.close = lambda: None
    monkeypatch.setattr(brief_smoke, "GmailReader", Reader)
    def fo_client():
        assert connected
        return fo
    monkeypatch.setattr(client, "LiveFoClient", fo_client)
    assert brief_smoke.main(args(tmp_path) + ["--yes-send"]) == 0
    assert brief_smoke.main(args(tmp_path) + ["--yes-send"]) == 0
    assert len(fo.asks) == 2
    saved = json.loads((tmp_path / "binding.json").read_text())[0]
    assert saved["outbound_message_id"] == "<copy@wajo.ai>" and saved["thread_id"] == "555"
    assert brief_smoke.main(args(tmp_path) + ["--audit-only"]) == 0
    assert len(fo.asks) == 2


def test_wrong_cc_account_is_rejected_before_a_fo_client_can_be_created(tmp_path, monkeypatch):
    import sealed.fo.client as client
    monkeypatch.setattr(brief_smoke, "load_env", lambda: None)
    def forbidden():
        raise AssertionError("Fo client created before checking the CC account")
    monkeypatch.setattr(client, "LiveFoClient", forbidden)
    class Reader:
        address = "different-audit-account@example.com"
        def __init__(self, *_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    monkeypatch.setattr(brief_smoke, "GmailReader", Reader)
    assert brief_smoke.main(args(tmp_path) + ["--yes-send"]) == 1
    assert not (tmp_path / "fo.json").exists()
