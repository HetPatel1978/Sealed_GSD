"""Holds each job's ceiling in process memory.

Only modules in sealed.private may call `_read`. The one way out of the private zone is `for_owner`, used only
by the `GET /jobs/{id}/ceiling` route so the user can see the ceiling they set (docs/api.md, decision 13).
Never log the value, and never put it in an event, a message, a Fo request or an LLM prompt.
"""

from math import isfinite

_ceilings: dict[str, float] = {}


def store(job_id: str, ceiling: float) -> None:
    if not isfinite(ceiling) or ceiling <= 0:
        raise ValueError("ceiling must be finite and positive")
    _ceilings[job_id] = float(ceiling)


def has(job_id: str) -> bool:
    return job_id in _ceilings


def forget(job_id: str) -> None:
    _ceilings.pop(job_id, None)


def _read(job_id: str) -> float:
    return _ceilings[job_id]


def for_owner(job_id: str) -> float | None:
    """The ceiling for the user's own dashboard only (GET /jobs/{id}/ceiling). None if the job has no ceiling."""
    return _ceilings.get(job_id)
