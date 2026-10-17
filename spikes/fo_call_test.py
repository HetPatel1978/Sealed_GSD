"""Spike 1d: ask Fo to place ONE outbound call to a team member's own phone and read a short, true, non-price script.

    backend/.venv/Scripts/python -u spikes/fo_call_test.py              # dry run: prints the instruction (phone masked)
    backend/.venv/Scripts/python -u spikes/fo_call_test.py --yes-call   # real call (only after a team member approves)

The phone number comes from SEALED_TEST_PHONE (set it in .env locally; never commit it).
Measures: confirmation first? does the call happen? does Fo stay on the script or add claims? does a
"voice" conversation appear, with a transcript or summary? end-to-end time.
Log: spikes/logs/call_<timestamp>.json (gitignored), phone number masked.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sealed.env import load_env  # noqa: E402

load_env()

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402

from sealed.fo.auth import FO_URL, FileTokenStorage, refresh_if_needed  # noqa: E402

SCRIPT = (
    "Hi, this is a test call. I'm Fo, an AI assistant calling on behalf of a buyer about one specific car. "
    "Could you please email the out-the-door total for that car to my email address? Thank you, goodbye."
)
STOP_WORDS = ("payment", "purchase", "paid", "charged", "order placed", "checkout")
LOG_DIR = Path(__file__).resolve().parent / "logs"
log: list[dict] = []


def instruction(phone: str) -> str:
    return (
        f"Please place one phone call to {phone}. This is a test call to my own phone.\n\n"
        "When someone answers, say only the following, then end the call politely:\n"
        f"\"{SCRIPT}\"\n\n"
        "Do not add any other claims or details. Do not agree to anything, do not make any commitment, "
        "purchase or payment, and do not mention any price. If you're asked a question you can't answer from "
        "this script, say you'll pass it on and end the call. "
        "After the call, tell me how it went and give me a transcript or a summary of the call."
    )


def mask(text: str, phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    masked = text.replace(phone, "<PHONE>")
    if len(digits) >= 7:
        masked = re.sub(r"[\d\-\s().+]{7,}", lambda m: "<PHONE>" if digits[-4:] in re.sub(r"\D", "", m.group()) else m.group(), masked)
    return masked


def record(phone: str, kind: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, **fields}
    entry = json.loads(mask(json.dumps(entry, default=str), phone))
    log.append(entry)
    print(json.dumps(entry, indent=2), flush=True)


async def run(phone: str) -> None:
    storage = FileTokenStorage()
    refresh_if_needed(storage)
    token = storage.load()["tokens"]["access_token"]
    t_start = time.perf_counter()
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                before = {c["conversationId"] for c in (await s.call_tool("list_fo_conversations", {})).structured_content.get("conversations", [])}

                async def turn(args: dict) -> dict:
                    record(phone, "request", tool="ask_fo", args=args)
                    t0 = time.perf_counter()
                    sc = (await s.call_tool("ask_fo", args, read_timeout_seconds=120)).structured_content or {}
                    record(phone, "response", tool="ask_fo", seconds=round(time.perf_counter() - t0, 1), result=sc)
                    polls = 0
                    while sc.get("status") == "working" and time.perf_counter() - t0 < 900:
                        polls += 1
                        t1 = time.perf_counter()
                        sc = (await s.call_tool("get_fo_reply", {"conversationId": sc["conversationId"], "waitSeconds": 40},
                                                read_timeout_seconds=120)).structured_content or {}
                        record(phone, "response", tool="get_fo_reply", seconds=round(time.perf_counter() - t1, 1), result=sc)
                    if any(w in (sc.get("response") or "").lower() for w in STOP_WORDS):
                        record(phone, "STOP_WARNING", note="Fo mentions payment/purchase/order. Stop and review.")
                    record(phone, "turn_done", status=sc.get("status"), polls=polls)
                    return sc

                sc = await turn({"message": instruction(phone), "newConversation": True})
                conv = sc.get("conversationId")
                text = sc.get("response") or ""
                if "?" in text:
                    digits = re.sub(r"\D", "", phone)
                    ok = digits[-4:] in re.sub(r"\D", "", text) and "car" in text.lower()
                    record(phone, "confirmation_asked", preview_has_number_and_script=ok)
                    if ok:
                        sc = await turn({"message": "Yes, place the call exactly as described.", "conversationId": conv})
                    else:
                        record(phone, "NOT_CONFIRMED", note="Preview didn't show the number + script; not answering yes.")

                after = (await s.call_tool("list_fo_conversations", {})).structured_content.get("conversations", [])
                new = [c for c in after if c["conversationId"] not in before]
                record(phone, "new_conversations", rows=new)
                for c in new:
                    r = await s.call_tool("get_fo_conversation", {"conversationId": c["conversationId"]})
                    record(phone, "transcript", channel=c.get("channel"), raw=r.structured_content)
    record(phone, "elapsed", seconds=round(time.perf_counter() - t_start, 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-call", action="store_true", help="actually place the call (only after a team member approves)")
    args = ap.parse_args()
    phone = os.getenv("SEALED_TEST_PHONE", "").strip()
    print("=== INSTRUCTION TO FO (phone masked) ===\n" + instruction("<SEALED_TEST_PHONE>"))
    if not args.yes_call:
        print("\nDry run only. Set SEALED_TEST_PHONE in .env and re-run with --yes-call after a team member approves.")
        return
    if not phone:
        sys.exit("SEALED_TEST_PHONE is not set")
    try:
        asyncio.run(run(phone))
    finally:
        LOG_DIR.mkdir(exist_ok=True)
        out = LOG_DIR / f"call_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(log, indent=2))
        print(f"log: {out}")


if __name__ == "__main__":
    main()
