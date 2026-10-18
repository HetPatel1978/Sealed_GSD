from sealed.private import vault
import pytest


def test_for_owner_returns_the_stored_ceiling_and_none_when_missing():
    vault.store("job_v", 32000.0)
    try:
        assert vault.for_owner("job_v") == 32000.0
    finally:
        vault.forget("job_v")
    assert vault.for_owner("job_v") is None


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_invalid_ceilings_are_rejected_without_echoing_the_value(value):
    with pytest.raises(ValueError, match="ceiling must be finite and positive"):
        vault.store("job_invalid", value)
    assert not vault.has("job_invalid")

