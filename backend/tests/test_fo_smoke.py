import pytest

from sealed.core.guard import Guard
from sealed.fo import smoke
from tests.helpers import FakeChecker
from tests.test_fo_adapter import FakeFo, ok


def test_dry_run_prints_the_exact_guarded_message_without_creating_a_client_or_state(tmp_path, monkeypatch, capsys):
    import sealed.fo.client as client
    monkeypatch.setattr(smoke, "load_env", lambda: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry run created a live client")
    monkeypatch.setattr(client, "LiveFoClient", forbidden)
    state = tmp_path / "state.json"
    assert smoke.main(["--to", "team@example.com", "--cc", "buyer@example.com", "--state", str(state)]) == 0
    assert "Dry run" in capsys.readouterr().out and not state.exists()
    assert Guard(FakeChecker()).check(smoke.build_message("team@example.com", "buyer@example.com", "smoke_01")).passed


def test_explicit_live_path_uses_the_adapter_and_suppresses_repeated_run_ids(tmp_path, monkeypatch):
    import sealed.fo.client as client
    monkeypatch.setattr(smoke, "load_env", lambda: None)
    fo = FakeFo([ok("Sent.")])
    fo.close = lambda: None
    monkeypatch.setattr(client, "LiveFoClient", lambda: fo)
    args = ["--to", "team@example.com", "--cc", "buyer@example.com", "--yes-send",
            "--state", str(tmp_path / "state.json")]
    assert smoke.main(args) == 0 and smoke.main(args) == 0
    assert len(fo.asks) == 1


@pytest.mark.parametrize("to,run_id", [("team@example.com\nBCC: other@example.com", "smoke"),
                                        ("bad-address", "smoke"), ("team@example.com", "bad\nrun")])
def test_smoke_rejects_header_injection_and_invalid_identifiers(to, run_id):
    with pytest.raises(ValueError):
        smoke.build_message(to, "buyer@example.com", run_id)
