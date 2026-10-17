import pytest

from sealed.private import vault
from sealed.core.numbers import numbers_in
from sealed.private.secret_checker import SecretChecker


@pytest.fixture
def checker():
    vault.store("job_t", 31500.0)
    yield SecretChecker("job_t")
    vault.forget("job_t")


@pytest.mark.parametrize("text", [
    "We can go up to $31,500.",
    "about 31500 out the door",
    "31.5k is fine",
    "roughly 31.5 thousand",
    "thirty-one thousand five hundred dollars",
    "Thirty one thousand",           # within 2% (31,000)
    "$32,100",                       # +1.9%
    "$30,900",                       # -1.9%
])
def test_flags_ceiling_and_near_values(checker, text):
    assert checker.contains_secret(text)


@pytest.mark.parametrize("text", [
    "The lowest written out-the-door total is $30,690.",
    "Could you do $29,500 out-the-door?",
    "a 2026 Honda Civic Sport near ZIP 19104",
    "within the next four weeks",
    "$32,200",                       # +2.2%
])
def test_passes_values_outside_band(checker, text):
    assert not checker.contains_secret(text)


def test_numbers_in_reads_words_and_digits():
    assert 31500.0 in numbers_in("thirty-one thousand five hundred")
    assert 31500.0 in numbers_in("$31,500")
    assert 31500.0 in numbers_in("31.5k")
