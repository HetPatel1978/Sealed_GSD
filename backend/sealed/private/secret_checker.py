"""Secret checker: text in, yes/no out. Flags any number in the text within ±2% of the job's ceiling.

Reads numbers written as digits ("$31,500", "31500", "31.5k", "31.5 thousand") and as English words
("thirty-one thousand five hundred"). Never returns or logs the ceiling.
"""

from sealed.core.numbers import numbers_in
from sealed.private import vault

BAND = 0.02


class SecretChecker:
    """Bound to one job. The guard holds this object; it can only ask yes/no questions."""

    def __init__(self, job_id: str):
        self._job_id = job_id

    def contains_secret(self, text: str) -> bool:
        ceiling = vault._read(self._job_id)
        return any(abs(n - ceiling) <= BAND * ceiling for n in numbers_in(text))
