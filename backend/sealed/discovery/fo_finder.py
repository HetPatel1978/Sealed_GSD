"""Primary search: one ask_fo turn in a NEW conversation (never a dealer's conversation).

Rules (docs/plan.md §3 step 6, threat model #40):
- The request holds only the car, the ZIP and the radius. Fo remembers across conversations, so nothing else.
- Fo is told to contact no one. A reply that suggests it emailed/called someone, or mentions money, is thrown
  away (`acted`) and flagged for a human; the backup search runs instead.
- The turn takes the account-wide Fo lock and gives up after `budget_s`, so it can't hold up sends for long.
- Fo answers in free text; we ask for one `|`-separated line per dealer and parse only those lines.
"""

import re
import threading
import time
from typing import Callable

from sealed.discovery.models import Candidate, FindResult
from sealed.fo.adapter import FO_TURN_LOCK, MONEY_ACTION, ask_turn, settle_turn
from sealed.fo.client import FoClient

BUDGET_S = 90
FIELDS = ("name", "website", "email", "phone", "address", "page")
EMPTY = {"", "-", "none", "n/a", "na", "unknown", "not listed", "not found", "null"}

ACTED = re.compile(r"\bi(?:'ve| have)? (?:just |already )?(?:emailed|called|phoned|texted|messaged|contacted|"
                   r"reached out|left (?:a )?(?:message|voicemail)|sent (?:an? )?(?:email|message|text))\b", re.I)


def request_text(car: str, zip_code: str, radius_miles: int) -> str:
    return (f"Find franchise dealers for a {car} within {radius_miles} miles of ZIP {zip_code}. "
            "This is research only: don't contact, email, call or message anyone, and don't fill in any forms. "
            "Reply with one line per dealer (up to 10), in exactly this format, using '-' for anything you "
            "can't find:\n"
            "name | website URL | sales email | sales phone | street address with ZIP | URL of the page "
            "where you found the email or phone")


def parse(text: str) -> list[Candidate]:
    out = []
    for line in text.splitlines():
        line = line.strip().strip("|").strip()
        parts = [p.strip().strip("*`").strip() for p in line.split("|")]
        if len(parts) != len(FIELDS):
            continue
        row = dict(zip(FIELDS, (None if p.lower() in EMPTY else p for p in parts)))
        if not row["name"] or set(line) <= set("|-: ") or row["name"].lower() == "name":
            continue                                      # markdown header / separator rows
        out.append(Candidate(name=re.sub(r"^\d+[.)]\s*", "", row["name"]), website=_url(row["website"]),
                             email=row["email"], phone=row["phone"], address=row["address"],
                             page_url=_url(row["page"]), found_by="fo"))
    return out


def _url(v: str | None) -> str | None:
    if not v:
        return None
    m = re.search(r"https?://[^\s)\]>]+", v)
    if m:
        return m.group(0).rstrip(".,")
    return f"https://{v.strip('/')}" if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", v, re.I) else None


class FoFinder:
    name = "fo"

    def __init__(self, client: FoClient | None, budget_s: float = BUDGET_S, wait_seconds: int = 40,
                 clock: Callable[[], float] = time.monotonic, lock: threading.Lock = FO_TURN_LOCK):
        self.client, self.budget_s, self.wait_seconds, self.clock, self.lock = (
            client, budget_s, wait_seconds, clock, lock)

    def find(self, car: str, zip_code: str, radius_miles: int) -> FindResult:
        if self.client is None:
            return FindResult(failure="not_configured", note="Fo is not connected.")
        errors: list[dict] = []
        transcript: list[dict] = []
        with self.lock:
            res = ask_turn(self.client, request_text(car, zip_code, radius_miles), None, errors, transcript)
            res = settle_turn(self.client, res, None, errors, transcript, self.budget_s, self.clock,
                              self.wait_seconds)
        status = res.get("status")
        text = res.get("response") or res.get("progress") or ""
        if status == "still_working":
            return FindResult(failure="timeout", note=f"Fo didn't finish within {self.budget_s:.0f}s.",
                              transcript=transcript)
        if status == "failed":
            return FindResult(failure="failed", note=text[:300] or "Fo returned an error.", transcript=transcript)
        if ACTED.search(text) or MONEY_ACTION.search(text):
            return FindResult(failure="acted", transcript=transcript,
                              note="Fo's reply suggests it contacted someone or mentions a payment. "
                                   "Results discarded; check the Fo conversation.")
        candidates = parse(text)
        if not candidates:
            return FindResult(failure="unparsed", note="No dealer lines in Fo's reply.", transcript=transcript)
        return FindResult(candidates=tuple(candidates), transcript=transcript)
