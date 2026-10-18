"""Shared types for discovery. A Candidate is what a search claims; only verify.py turns it into a result."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:
    name: str
    website: str | None
    email: str | None
    phone: str | None
    address: str | None
    page_url: str | None            # page the search says the contact details came from
    found_by: str                   # "fo" | "claude"
    in_stock: bool | None = None


@dataclass(frozen=True)
class FindResult:
    """`failure` is None on success, else "failed" | "timeout" | "unparsed" | "acted" | "not_configured"."""
    candidates: tuple[Candidate, ...] = ()
    failure: str | None = None
    note: str | None = None
    transcript: list[dict] = field(default_factory=list)


class DiscoveryUnavailable(Exception):
    """Every search failed and nothing was verified. The route returns 503 discovery_unavailable."""
