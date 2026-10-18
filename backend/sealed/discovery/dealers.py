"""POST /dealers/discover: run the searches in order (Fo first, Claude as backup), verify, merge.

The backup runs when the first search fails (error, timeout, unreadable reply, not connected), when Fo's
reply suggests it acted beyond "find", or when fewer than MIN_RESULTS dealers survive the checks.
"""

import os
from typing import Callable

from sealed.discovery import verify
from sealed.discovery.fetch import Fetcher
from sealed.discovery.models import DiscoveryUnavailable

RADII = (20,)            # 10 and 50 come after the freeze (docs/plan.md)
MIN_RESULTS = 3
MAX_RESULTS = 10


def order_from_env() -> tuple[str, ...]:
    raw = os.getenv("SEALED_DISCOVERY_ORDER") or "fo,claude"
    return tuple(n.strip() for n in raw.split(",") if n.strip())


def discover(car: str, zip_code: str, finders: list, fetcher: Fetcher, radius_miles: int = 20,
             distance: Callable[[str, str], float | None] = verify.zip_distance_miles) -> dict:
    if radius_miles not in RADII:
        raise ValueError(f"radius_miles must be one of {RADII}")
    if not finders:
        raise DiscoveryUnavailable("no search configured")

    dealers: list[dict] = []
    dropped = 0
    fallback_reason = None
    warnings: list[dict] = []
    used: list[str] = []
    failures: list[str | None] = []
    for i, finder in enumerate(finders):
        used.append(finder.name)
        res = finder.find(car, zip_code, radius_miles)
        if res.failure == "acted":
            warnings.append({"code": "needs_human", "search": finder.name, "message": res.note})
        verified = []
        for c in res.candidates:
            record = verify.check(c, zip_code, radius_miles, fetcher, distance)
            if record is None:
                dropped += 1
            else:
                verified.append(record)
        dealers = verify.merge(dealers, verified)
        failures.append(res.failure)
        if res.failure is None and len(dealers) >= MIN_RESULTS:
            break
        if i == 0 and len(finders) > 1:
            fallback_reason = f"{finder.name}_{res.failure}" if res.failure else "too_few"

    if not dealers and all(failures):
        raise DiscoveryUnavailable(", ".join(f"{n}_{f}" for n, f in zip(used, failures)))
    dealers.sort(key=lambda d: d["distance_miles"])
    return {
        "zip": zip_code, "radius_miles": radius_miles,
        "dealers": dealers[:MAX_RESULTS],
        "dropped": dropped,
        "search": {"primary": finders[0].name, "fallback_used": len(used) > 1,
                   "fallback_reason": fallback_reason, "warnings": warnings},
    }
