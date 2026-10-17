"""Pre-send guard. Runs on our text (stage pre_send) and on Fo's confirmation preview (stage fo_preview).

It never sees the ceiling: it holds a checker object that only answers yes/no.
Result rules match docs/api.md: secret_checker, limit_language, leverage_grounded, commitment_words,
safety_line (+ preview_matches_ours at the fo_preview stage). Details never contain budget values.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from sealed.core.models import Outbound
from sealed.fo.messages import DISCLOSURE, SAFETY_PHRASES

BUDGET_WORDS = re.compile(r"\b(budget|ceiling|max(imum)?\s+(price|budget)|afford|price\s+limit|walk[- ]?away|"
                          r"most\s+(we|he|she|they|i)\s+(can|could|will|would)\s+pay|top\s+dollar)\b", re.I)
COMMIT_WORDS = re.compile(r"\b(accept\w*|agree\w*|confirm\w*|order\w*|pay|paid|payment\w*|deposit\w*|"
                          r"purchase\w*|sign\w*|commit\w*|proceed\w*|reserve\w*|hold\s+the\s+car)\b", re.I)
MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")


class Checker(Protocol):
    def contains_secret(self, text: str) -> bool: ...


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    stage: str
    rules: list[dict]


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def money_values(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else "")) for m in MONEY.finditer(text)]


def mask_allowed(text: str, allowed: frozenset[float]) -> str:
    """Replaces "$X" money values that are in `allowed` with a neutral token. Everything else stays."""
    def sub(m: re.Match) -> str:
        value = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
        return "[allowed price]" if value in allowed else m.group(0)
    return MONEY.sub(sub, text)


class Guard:
    def __init__(self, checker: Checker):
        self._checker = checker

    def check(self, msg: Outbound, preview: str | None = None) -> GuardResult:
        stage = "fo_preview" if preview is not None else "pre_send"
        text = preview if preview is not None else msg.body
        rules = []

        # Allowed prices (ledger quotes, the target, the approved price) are true and reveal nothing, even near
        # the ceiling; blocking them would make our behaviour depend on the ceiling. Mask them, check the rest.
        rules.append({"rule": "secret_checker",
                      "passed": not self._checker.contains_secret(mask_allowed(text, msg.allowed_prices))})
        scrubbed = text
        for phrase in SAFETY_PHRASES:       # fixed safety text (incl. call hard limits) says what we WON'T do
            scrubbed = scrubbed.replace(phrase, "")
        rules.append({"rule": "limit_language", "passed": not BUDGET_WORDS.search(scrubbed)})

        unknown = [v for v in money_values(text) if v not in msg.allowed_prices]
        grounded = {"rule": "leverage_grounded", "passed": not unknown}
        if unknown:
            grounded["detail"] = f"{len(unknown)} price(s) not from the ledger or the target"
        elif msg.cited_quote_id:
            grounded["detail"] = f"cites written quote {msg.cited_quote_id}"
        rules.append(grounded)

        commits = COMMIT_WORDS.findall(scrubbed)
        commit_rule = {"rule": "commitment_words", "passed": not commits or msg.booking_approved}
        if commits and msg.booking_approved:
            commit_rule["detail"] = "allowed: user-approved booking"
        elif commits:
            commit_rule["detail"] = "commitment wording outside an approved booking"
        rules.append(commit_rule)

        rules.append({"rule": "safety_line", "passed": DISCLOSURE in text})

        if preview is not None:
            rules.append({"rule": "preview_matches_ours", "passed": _squash(msg.body) in _squash(preview)})

        return GuardResult(all(r["passed"] for r in rules), stage, rules)
