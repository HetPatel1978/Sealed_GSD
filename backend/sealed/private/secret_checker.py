"""Secret checker: text in, yes/no out. Flags any number in the text within ±2% of the job's ceiling.

Reads numbers written as digits ("$31,500", "31500", "31.5k", "31.5 thousand") and as English words
("thirty-one thousand five hundred"). Never returns or logs the ceiling.
"""

import re

from sealed.private import vault

BAND = 0.02

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALES = {"hundred": 100, "thousand": 1000, "grand": 1000, "k": 1000, "million": 1_000_000}
_DIGITS = re.compile(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s?(k|thousand|grand)?\b", re.I)


def numbers_in(text: str) -> list[float]:
    """Every number in the text, from digits or English words."""
    out: list[float] = []
    for m in _DIGITS.finditer(text):
        value = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
        if m.group(3):
            value *= 1000
        out.append(value)
    out.extend(_word_numbers(text))
    return out


def _word_numbers(text: str) -> list[float]:
    tokens = re.findall(r"[a-z]+", text.lower().replace("-", " "))
    found, total, current, active = [], 0, 0, False
    for tok in tokens + ["<end>"]:
        if tok in _UNITS:
            current += _UNITS[tok]
            active = True
        elif tok in _TENS:
            current += _TENS[tok]
            active = True
        elif tok == "hundred" and active:
            current *= 100
        elif tok in _SCALES and active:
            total += current * _SCALES[tok]
            current = 0
        elif tok == "and" and active:
            continue
        else:
            if active:
                found.append(float(total + current))
            total, current, active = 0, 0, False
    return found


class SecretChecker:
    """Bound to one job. The guard holds this object; it can only ask yes/no questions."""

    def __init__(self, job_id: str):
        self._job_id = job_id

    def contains_secret(self, text: str) -> bool:
        ceiling = vault._read(self._job_id)
        return any(abs(n - ceiling) <= BAND * ceiling for n in numbers_in(text))
