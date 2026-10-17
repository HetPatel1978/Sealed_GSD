"""Validates mock/events.json. Exits non-zero on any problem.

Checks:
  1. Nothing looks like a ceiling: no key or text value naming a ceiling/budget/max price
     (only the trust counter `budget_revealed_count` is allowed).
  2. Optional: --secret N  fails if the number N, or any number within 2% of it, appears anywhere.
     Pass it on the command line only; never write the value into the repo.
  3. Envelope shape, increasing ids, non-decreasing timestamps, every event type present, mode "sim".
  4. job.completed numbers add up.

Usage:  python mock/check_mock.py [path] [--secret N]
"""

import argparse
import json
import re
import sys
from pathlib import Path

ENVELOPE = {"id", "run_id", "job_id", "ts", "type", "vendor_id", "mode", "data"}
TYPES = {
    "job.created", "job.started", "vendor.contacted", "guard.checked", "message.sent", "audit.checked",
    "reply.received", "quote.parsed", "quote.verified", "strategy.decided", "proposal.created",
    "approval.requested", "job.approved", "job.rejected", "job.needs_human", "job.completed", "error",
}
BAD_KEY = re.compile(r"ceil|budget|max_?price|max_?budget|walk_?away|reserve|limit", re.I)
BAD_TEXT = re.compile(r"ceiling|budget|max(imum)?[ _-]?price|walk[ -]?away|reserve price|price limit", re.I)
ALLOWED_KEYS = {"budget_revealed_count"}
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

problems: list[str] = []


def walk(node, path, secret):
    if isinstance(node, dict):
        for k, v in node.items():
            if k not in ALLOWED_KEYS and BAD_KEY.search(k):
                problems.append(f"{path}.{k}: key looks like a ceiling field")
            walk(v, f"{path}.{k}", secret)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]", secret)
    elif isinstance(node, str):
        if BAD_TEXT.search(node):
            problems.append(f"{path}: text mentions a ceiling/budget: {node[:60]!r}")
        if secret is not None:
            for m in NUM.findall(node):
                check_number(float(m.replace(",", "")), path, secret)
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and secret is not None:
        check_number(float(node), path, secret)


def check_number(x, path, secret):
    if secret and abs(x - secret) / secret <= 0.02:
        problems.append(f"{path}: value {x} is within 2% of the secret")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(Path(__file__).with_name("events.json")))
    ap.add_argument("--secret", type=float, default=None)
    args = ap.parse_args()

    events = json.loads(Path(args.path).read_text(encoding="utf-8"))
    walk(events, "$", args.secret)

    seen, last_id, last_ts = set(), 0, ""
    for e in events:
        if set(e) != ENVELOPE:
            problems.append(f"event {e.get('id')}: envelope keys {sorted(e)}")
            continue
        if e["id"] <= last_id:
            problems.append(f"event {e['id']}: id not increasing")
        if e["ts"] < last_ts:
            problems.append(f"event {e['id']}: timestamp goes backwards")
        if e["mode"] != "sim":
            problems.append(f"event {e['id']}: mode is {e['mode']!r}, expected 'sim'")
        if e["type"] not in TYPES:
            problems.append(f"event {e['id']}: unknown type {e['type']}")
        last_id, last_ts = e["id"], e["ts"]
        seen.add(e["type"])
    for t in sorted(TYPES - seen):
        problems.append(f"missing event type: {t}")

    done = [e for e in events if e["type"] == "job.completed"]
    if len(done) != 1:
        problems.append(f"expected exactly 1 job.completed, found {len(done)}")
    else:
        d = done[0]["data"]
        if abs(d["savings_abs"] - (d["first_quote"] - d["best_quote"])) > 0.005:
            problems.append("job.completed: savings_abs != first_quote - best_quote")
        if abs(d["savings_pct"] - round(100 * d["savings_abs"] / d["first_quote"], 2)) > 0.005:
            problems.append("job.completed: savings_pct does not match")
        guards = sum(e["type"] == "guard.checked" for e in events)
        audits = sum(e["type"] == "audit.checked" for e in events)
        sent = sum(e["type"] == "message.sent" for e in events)
        if d["trust"]["messages_checked_before"] != guards or guards != sent:
            problems.append("trust: messages_checked_before != guard.checked count / message.sent count")
        if d["trust"]["messages_checked_after"] != audits:
            problems.append("trust: messages_checked_after != audit.checked count")
        approved = [e for e in events if e["type"] == "job.approved"]
        if not approved or approved[-1]["data"]["price_total"] != d["best_quote"]:
            problems.append("best_quote does not match the approved price")

    if problems:
        print(f"FAIL: {len(problems)} problem(s)")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print(f"OK: {len(events)} events, {len(seen)} event types, no ceiling-like fields"
          + (", secret value absent (+/-2%)" if args.secret is not None else ""))


if __name__ == "__main__":
    main()
