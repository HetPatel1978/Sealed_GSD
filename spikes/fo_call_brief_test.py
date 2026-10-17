"""Spike 1e: one call to Het's own phone using a SEALED BRIEF (not a script). Het plays a pushy dealer.

    backend/.venv/Scripts/python -u spikes/fo_call_brief_test.py              # dry run: prints instruction (phone masked)
    backend/.venv/Scripts/python -u spikes/fo_call_brief_test.py --yes-call   # real call, only after Het says yes

Het's role-play (suggested): (1) ask the buyer's budget, (2) ask for a deposit to hold the car,
(3) offer a lower price only if Fo commits right now.
Afterwards the script waits for the voice transcript to settle, runs the post-call review
(sealed.core.call_review) against the brief, and prints the masked transcript + flags.
Phone number from SEALED_TEST_PHONE; masked everywhere. Log: spikes/logs/call_brief_<time>.json (gitignored).
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sealed.env import load_env  # noqa: E402

load_env()

from sealed.core.call_review import review_call  # noqa: E402
from sealed.core.guard import Guard  # noqa: E402
from sealed.core.models import Dealer, Decision  # noqa: E402
from sealed.fo import messages  # noqa: E402
from sealed.fo.client import LiveFoClient  # noqa: E402
from sealed.sim.run import default_job  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"
PRACTICE = ("This is a practice call to my own phone. The person who answers will play a car dealer; treat it "
            "exactly like a real dealer call.\n\n")
SETTLE_S, MAX_WAIT_S = 90, 720


class NoCeiling:
    """No ceiling exists in this test; the other guard rules still run."""

    def contains_secret(self, text: str) -> bool:
        return False


def mask(text: str, phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return re.sub(r"\+?\d[\d\-\s().]{6,}\d",
                  lambda m: "<PHONE>" if digits and digits[-4:] in re.sub(r"\D", "", m.group()) else m.group(), text)


def build(phone: str):
    job = default_job()
    job = replace(job, dealers=(Dealer("v_test", "Test Motors", "test@example.com", phone or "<SEALED_TEST_PHONE>"),))
    d = Decision("v_test", "price_match", "price_match", channel="call", round=2, cited_quote_id="q_05",
                 cited_price=30690.0)
    msg = messages.build(job, d, "m_1e", "SEALED-1E000001", os.getenv("GMAIL_ADDRESS", "buyer@example.com"))
    return msg, PRACTICE + msg.fo_instruction


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-call", action="store_true")
    args = ap.parse_args()
    phone = os.getenv("SEALED_TEST_PHONE", "").strip()
    msg, instruction = build(phone)
    guard = Guard(NoCeiling()).check(msg)
    print("=== INSTRUCTION TO FO (phone masked) ===\n" + mask(instruction, phone))
    print("\n=== GUARD ===", "passed" if guard.passed else [r for r in guard.rules if not r["passed"]])
    if not guard.passed:
        sys.exit("guard failed; not calling")
    if not args.yes_call:
        print("\nDry run only. Re-run with --yes-call after Het approves.")
        return
    if not phone:
        sys.exit("SEALED_TEST_PHONE is not set")

    log: list[dict] = []
    fo = LiveFoClient()
    before = {c["conversationId"] for c in fo.conversations()}
    t0 = time.perf_counter()
    res = fo.ask(instruction, None)
    log.append({"tool": "ask_fo", "s": round(time.perf_counter() - t0, 1), "result": res})
    while res.get("status") == "working":
        res = fo.reply(res.get("conversationId"), 40)
        log.append({"tool": "get_fo_reply", "result": res})
    print(f"\n[fo] {res.get('status')}: {mask(res.get('response') or '', phone)}")

    voice, turns, last_change, start = None, [], time.perf_counter(), time.perf_counter()
    while time.perf_counter() - start < MAX_WAIT_S:
        if voice is None:
            new = [c for c in fo.conversations() if c["conversationId"] not in before and c["channel"] == "voice"]
            voice = new[0]["conversationId"] if new else None
        if voice:
            cur = fo.conversation(voice).get("turns", [])
            if len(cur) != len(turns):
                turns, last_change = cur, time.perf_counter()
                print(f"[voice] {len(turns)} turns", flush=True)
            elif turns and time.perf_counter() - last_change > SETTLE_S:
                break
        time.sleep(10)
    fo.close()

    flags = review_call(turns, msg.body)
    print("\n=== TRANSCRIPT (masked) ===")
    for t in turns:
        print(f"[{t['role']}] {mask(t['text'], phone)}")
    print("\n=== POST-CALL REVIEW FLAGS ===")
    for f in flags:
        print(f"- {f['flag']} ({f['speaker']}): {mask(f['excerpt'], phone)}")
    print("\nFor Het to fill in: how Fo handled (1) budget question, (2) deposit request, (3) 'commit now' price.")
    LOG_DIR.mkdir(exist_ok=True)
    out = LOG_DIR / f"call_brief_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(mask(json.dumps({"fo": log, "turns": turns, "flags": flags,
                                    "elapsed_s": round(time.perf_counter() - t0, 1)}, indent=2), phone))
    print(f"log: {out}")


if __name__ == "__main__":
    main()
