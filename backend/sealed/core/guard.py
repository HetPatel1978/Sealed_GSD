"""Check public briefs, Fo-composed drafts and actual sent content.

Brief-mode email validates constraints and price roles, allowing natural wording.
Legacy exact email/call previews retain their strict payload checks.

It never sees the ceiling: it holds a checker object that only answers yes/no.
Result rules match docs/api.md: secret_checker, limit_language, leverage_grounded, commitment_words,
safety_line (+ preview_matches_ours at the fo_preview stage). Details never contain budget values.
"""

import re
from dataclasses import dataclass, replace
from typing import Protocol

from sealed.core.models import EmailDraft, Outbound
from sealed.core.email_policy import brief_authorized, mask_known_prices, parse_draft, review_rules
from sealed.fo.messages import (DISCLOSURE, EMAIL_LIMITS, SAFETY_PHRASES, render_email_brief,
                                wrap_call_for_fo, wrap_email_brief_for_fo, wrap_for_fo)

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


def preview_matches(msg: Outbound, preview: str) -> bool:
    """Accept the exact payload with a small set of known confirmation wrappers.

    Arbitrary text before/after the body is never approved. If headers are shown, they must match too.
    An unfamiliar wrapper pauses sending rather than asking an LLM to interpret it.
    """
    prefixes = ("", "Here's the draft: ", "Here is the draft: ",
                f"Here's what I'll send to {msg.dealer_name}: ")
    suffixes = ("", " Good to send?", " Should I send it?", " Shall I send it?",
                " Ready to send?", " Okay to send?", " OK to send?")
    if msg.channel == "call":
        prefixes = ("", "Here's the call brief: ", "Here is the call brief: ")
        suffixes = ("", " Good to call?", " Shall I place the call?", " Okay to call?")
    bodies = [msg.body]
    if msg.channel == "email":
        bodies.append(f"To: {msg.to}\nCC: {msg.cc}\nSubject: {msg.subject}\nBody: {msg.body}")
    return _squash(preview) in {_squash(p + body + s) for p in prefixes for body in bodies for s in suffixes}


def payload_intact(msg: Outbound) -> bool:
    headers = (msg.to, msg.cc, msg.subject or "", msg.dealer_name)
    if any("\r" in v or "\n" in v for v in headers):
        return False
    if any(marker in v for v in (*headers, msg.body)
           for marker in ("<<<EMAIL_BODY", "EMAIL_BODY>>>", "<<<CALL_BRIEF", "CALL_BRIEF>>>",
                          "<<<EMAIL_BRIEF", "EMAIL_BRIEF>>>", "<<<EMAIL_DRAFT", "EMAIL_DRAFT>>>")):
        return False
    if msg.channel == "email":
        if msg.email_brief is not None:
            try:
                if msg.reply_to_message_id is not None and not re.fullmatch(r"<[A-Za-z0-9._+@=/-]+>", msg.reply_to_message_id):
                    return False
                if msg.reply_thread_id is not None and not msg.reply_thread_id.isdigit():
                    return False
                return (brief_authorized(msg) and msg.body == render_email_brief(msg.kind, msg.email_brief)
                        and msg.fo_instruction == wrap_email_brief_for_fo(msg.to, msg.cc, msg.subject or "", msg.body,
                                                                         msg.reply_to_message_id))
            except (KeyError, ValueError):
                return False
        return _squash(msg.fo_instruction) == _squash(wrap_for_fo(msg.to, msg.cc, msg.subject or "", msg.body))
    if msg.channel == "call":
        if msg.call_buyer is not None:
            from sealed.fo.messages import call_plan_request
            return (bool(msg.to and msg.call_buyer.strip())
                    and not any(c in msg.call_buyer for c in "\r\n\x00")
                    and msg.fo_instruction == call_plan_request(msg))
        return _squash(msg.fo_instruction) == _squash(wrap_call_for_fo(msg.dealer_name, msg.to, msg.body))
    return False


class Guard:
    def __init__(self, checker: Checker):
        self._checker = checker

    def safe_note(self, msg: Outbound, note: str | None) -> str | None:
        """Never copy an untrusted draft or a suspected secret into the event log."""
        if not note or (msg.email_brief is not None and "<<<EMAIL_DRAFT" in note):
            return None
        masked = mask_known_prices(note, msg.allowed_prices) if msg.email_brief is not None else mask_allowed(note, msg.allowed_prices)
        return None if self._checker.contains_secret(masked) or BUDGET_WORDS.search(note) else note[:500]

    def check(self, msg: Outbound, preview: str | None = None) -> GuardResult:
        if msg.call_buyer is not None and preview is not None:
            from sealed.core.call_policy import plan_matches
            rules = [*self.check(msg).rules, {"rule": "call_plan_matches", "passed": plan_matches(msg, preview)}]
            return GuardResult(all(rule["passed"] for rule in rules), "fo_preview", rules)
        if msg.email_brief is not None and preview is not None:
            draft = parse_draft(preview)
            if draft is None:
                # Scan rejected raw previews too; no secret value or excerpt is returned.
                return GuardResult(False, "fo_preview", [
                    {"rule": "secret_checker", "passed": not self._checker.contains_secret(mask_known_prices(preview, msg.allowed_prices))},
                    {"rule": "draft_format", "passed": False}])
            return self.check_email(msg, draft, stage="fo_preview")
        stage = "fo_preview" if preview is not None else "pre_send"
        text = preview if preview is not None else msg.body
        # Fo receives headers and instructions as well as the body. Check all of them for secrets/prices.
        payload = "\n".join((msg.to, msg.cc, msg.subject or "", msg.body, msg.fo_instruction, preview or ""))
        rules = []

        # Allowed prices (ledger quotes, the target, the approved price) are true and reveal nothing, even near
        # the ceiling; blocking them would make our behaviour depend on the ceiling. Mask them, check the rest.
        rules.append({"rule": "secret_checker",
                      "passed": not self._checker.contains_secret(mask_allowed(payload, msg.allowed_prices))})
        scrubbed = "\n".join((text, msg.subject or ""))
        # Mailbox names such as orders@... are routing metadata, not commitment wording.
        for address in (msg.to, msg.cc):
            if address:
                scrubbed = scrubbed.replace(address, "[address]")
        for phrase in SAFETY_PHRASES:       # fixed safety text (incl. call hard limits) says what we WON'T do
            scrubbed = scrubbed.replace(phrase, "")
        if msg.email_brief is not None:
            scrubbed = scrubbed.replace(EMAIL_LIMITS, "")
            scrubbed = scrubbed.replace("No payment is being made by email.", "")
        rules.append({"rule": "limit_language", "passed": not BUDGET_WORDS.search(scrubbed)})

        unknown = {v for v in money_values(payload) if v not in msg.allowed_prices}
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
        rules.append({"rule": "payload_integrity", "passed": payload_intact(msg)})

        if preview is not None:
            rules.append({"rule": "preview_matches_ours", "passed": preview_matches(msg, preview)})

        return GuardResult(all(r["passed"] for r in rules), stage, rules)

    def check_email(self, msg: Outbound, draft: EmailDraft, *, stage: str = "post_send", extra_headers: str = "") -> GuardResult:
        """Review a full actual draft/sent copy against a brief, not against exact wording."""
        payload = "\n".join((draft.to, draft.cc, draft.subject, draft.body, extra_headers))
        rules = [
            {"rule": "secret_checker", "passed": not self._checker.contains_secret(mask_known_prices(payload, msg.allowed_prices))},
            {"rule": "limit_language", "passed": not BUDGET_WORDS.search(payload)},
            {"rule": "leverage_grounded", "passed": not {v for v in money_values(payload) if v not in msg.allowed_prices}},
            {"rule": "payload_integrity", "passed": payload_intact(msg)},
            *review_rules(msg, draft),
        ]
        return GuardResult(all(r["passed"] for r in rules), stage, rules)

    def check_delivered(self, msg: Outbound, copy: EmailDraft, *, extra_headers: str = "") -> GuardResult:
        """Full actual content, preserving legacy exact-send safety semantics."""
        if msg.email_brief is not None:
            return self.check_email(msg, copy, extra_headers=extra_headers)
        actual = replace(msg, to=copy.to, cc=copy.cc, subject=copy.subject, body=copy.body,
                         fo_instruction=wrap_for_fo(copy.to, copy.cc, copy.subject, copy.body))
        result = self.check(actual)
        for rule in result.rules:
            if rule["rule"] == "secret_checker":
                rule["passed"] &= not self._checker.contains_secret(mask_allowed(extra_headers, msg.allowed_prices))
            elif rule["rule"] == "limit_language":
                rule["passed"] &= not bool(BUDGET_WORDS.search(extra_headers))
            elif rule["rule"] == "leverage_grounded":
                rule["passed"] &= not {v for v in money_values(extra_headers) if v not in msg.allowed_prices}
        return GuardResult(all(r["passed"] for r in result.rules), "post_send", result.rules)
