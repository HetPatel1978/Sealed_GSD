"""Post-call review of Fo's voice transcript against the sealed brief it was given.

Flags (each becomes part of a job.needs_human event):
  - number_outside_brief: Fo said a number that is not in the brief
  - commitment: Fo used agreement/commitment wording that isn't negated ("I can't agree…" is fine)
  - money_topic: anyone mentioned a budget, maximum, deposit, payment or financing
Prices the dealer says are recorded separately as quotes with firm: false; they are not flags.
"""

import re

from sealed.core.numbers import numbers_in

COMMIT = re.compile(r"\b(agree\w*|accept\w*|reserv\w*|hold(ing)?\b|order\w*|pay(ing|s)?\b|deposit\w*|"
                    r"purchas\w*|commit\w*|confirm\w*|lock(ed)? in|sign\w*|deal\b)", re.I)
NEGATED = re.compile(r"\b(not|no|never|can'?t|cannot|won'?t|unable to|don'?t|isn'?t|aren'?t|without)\b"
                     r"(\W+\w+){0,4}\W*$", re.I)
MONEY_TOPIC = re.compile(r"\b(budget|maximum|max price|afford\w*|deposit\w*|payment\w*|pay\b|paying|"
                         r"credit card|card number|financ\w*|down payment|monthly|"
                         r"(looking|able|willing|want(ing)?|going) to spend|can spend|how much .{0,30}spend)\b", re.I)
FO_ROLES = {"assistant", "fo"}


def review_call(turns: list[dict], brief: str) -> list[dict]:
    allowed = set(numbers_in(brief))
    flags: list[dict] = []
    for i, turn in enumerate(turns):
        text, role = turn.get("text", ""), turn.get("role", "")
        speaker = "fo" if role in FO_ROLES else "dealer"
        if speaker == "fo":
            for n in numbers_in(text):
                if n not in allowed:
                    flags.append({"flag": "number_outside_brief", "turn": i, "speaker": speaker,
                                  "excerpt": _excerpt(text, str(int(n)) if n.is_integer() else str(n))})
            for m in COMMIT.finditer(text):
                if not NEGATED.search(text[max(0, m.start() - 60):m.start()]):
                    flags.append({"flag": "commitment", "turn": i, "speaker": speaker,
                                  "excerpt": _excerpt(text, m.group(0))})
        for m in MONEY_TOPIC.finditer(text):
            flags.append({"flag": "money_topic", "turn": i, "speaker": speaker, "excerpt": _excerpt(text, m.group(0))})
    return flags


def _excerpt(text: str, needle: str, width: int = 70) -> str:
    i = text.lower().find(needle.lower().replace(",", ""))
    i = max(i, 0)
    return text[max(0, i - width // 2): i + len(needle) + width // 2].strip()
