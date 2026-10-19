import json

import pytest

from sealed.fo.adapter import Store
from sealed.fo.review import ReviewError, main, review_call
from sealed.service.lock import worker_lock


@pytest.fixture
def paused(tmp_path):
    path = tmp_path / "fo.json"
    state = Store(path)
    key = Store.key("job_one", "run_one", "m_01")
    state.data["conversations"][Store.key("job_one", "run_one", "v_01")] = "call-conversation"
    state.set_status(key, "unclear", active=True, channel="call", reason_code="send_unclear",
                     fingerprint="original-payload-hash", dealer_id="v_01", conversation="call-conversation",
                     fo_reply="Calling now; result uncertain.")
    confirmation = tmp_path / "confirmation.txt"
    confirmation.write_text("Fo confirmation reviewed: call ended and no scheduled follow-ups remain.", encoding="utf-8")
    return path, key, confirmation


def release(paused, **overrides):
    path, _, confirmation = paused
    args = {"job_id": "job_one", "message_id": "m_01", "confirmation_file": confirmation,
            "call_finished": True, "no_followups": True, **overrides}
    return review_call(path, **args)


def test_review_releases_account_preserves_original_attempt_and_does_not_resume(paused):
    path, key, _ = paused
    before = Store(path).data
    assert release(paused) == {"released": True, "job_resumed": False, "outreach_repeated": False}
    after = Store(path).data
    assert after["active"] is None
    assert after["conversations"] == before["conversations"]
    record = after["messages"][key]
    assert {k:v for k,v in record.items() if k != "review"} == before["messages"][key]
    assert record["review"]["no_pending_followups"] and len(record["review"]["confirmation_sha256"]) == 64
    assert "Fo confirmation reviewed" not in json.dumps(after)


@pytest.mark.parametrize("change", [{"job_id": "another_job"}, {"message_id": "m_02"},
                                    {"call_finished": False}, {"no_followups": False}])
def test_wrong_scope_or_missing_attestation_keeps_reservation_unchanged(paused, change):
    path, _, _ = paused
    before = path.read_bytes()
    with pytest.raises(ReviewError):
        release(paused, **change)
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", [{"channel": "email"}, {"status": "pending"},
                                    {"reason_code": "payment_mention"}, {"conversation": "other-conversation"}])
def test_other_outcomes_cannot_be_released_as_an_uncertain_call(paused, change):
    path, key, _ = paused
    store = Store(path)
    store.data["messages"][key].update(change)
    store.save()
    before = path.read_bytes()
    with pytest.raises(ReviewError):
        release(paused)
    assert path.read_bytes() == before


def test_running_worker_blocks_review(paused):
    path, _, _ = paused
    before = path.read_bytes()
    with worker_lock(path.with_suffix(path.suffix + ".worker.lock")):
        with pytest.raises(RuntimeError, match="coordinator_already_running"):
            release(paused)
    assert path.read_bytes() == before


def test_confirmation_required_and_repeated_review_is_noop(paused):
    path, _, confirmation = paused
    before = path.read_bytes()
    confirmation.write_text("", encoding="utf-8")
    with pytest.raises(ReviewError):
        release(paused)
    assert path.read_bytes() == before
    confirmation.write_text("Reviewed confirmation that no Fo follow-ups remain.", encoding="utf-8")
    release(paused)
    reviewed = path.read_bytes()
    with pytest.raises(ReviewError):
        release(paused)
    assert path.read_bytes() == reviewed


def test_cli_requires_review_flags_and_constructs_no_provider(paused, capsys):
    path, _, confirmation = paused
    before = path.read_bytes()
    result = main(["--fo-state", str(path), "--job-id", "job_one", "--message-id", "m_01",
                   "--confirmation-file", str(confirmation)])
    assert result == 1 and path.read_bytes() == before
    assert "Confirm" in capsys.readouterr().out
