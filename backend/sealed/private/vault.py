"""Holds each job's ceiling in process memory. Write-only from outside the private zone.

Only modules in sealed.private may call `_read`. Never log or return the value.
"""

_ceilings: dict[str, float] = {}


def store(job_id: str, ceiling: float) -> None:
    if ceiling <= 0:
        raise ValueError("ceiling must be positive")
    _ceilings[job_id] = float(ceiling)


def has(job_id: str) -> bool:
    return job_id in _ceilings


def forget(job_id: str) -> None:
    _ceilings.pop(job_id, None)


def _read(job_id: str) -> float:
    return _ceilings[job_id]
