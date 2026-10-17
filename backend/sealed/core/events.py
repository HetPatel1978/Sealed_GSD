"""Append-only event log in the docs/api.md envelope. In-memory for now (SQLite + SSE come in step 5)."""

from datetime import datetime, timedelta, timezone


class EventLog:
    def __init__(self, job_id: str, run_id: str, mode: str, t0: datetime | None = None):
        self.job_id, self.run_id, self.mode = job_id, run_id, mode
        self.t0 = t0 or datetime(2026, 10, 17, 14, 0, 0, tzinfo=timezone.utc)
        self.events: list[dict] = []
        self._last_ts = ""

    def emit(self, minutes: float, type_: str, vendor_id: str | None, data: dict) -> dict:
        ts = (self.t0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts = max(ts, self._last_ts)  # never go backwards
        self._last_ts = ts
        e = {"id": len(self.events) + 1, "run_id": self.run_id, "job_id": self.job_id, "ts": ts,
             "type": type_, "vendor_id": vendor_id, "mode": self.mode, "data": data}
        self.events.append(e)
        return e

    def of_type(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]
