from sealed.private import vault


def test_for_owner_returns_the_stored_ceiling_and_none_when_missing():
    vault.store("job_v", 32000.0)
    try:
        assert vault.for_owner("job_v") == 32000.0
    finally:
        vault.forget("job_v")
    assert vault.for_owner("job_v") is None

