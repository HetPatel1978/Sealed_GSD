"""Spike 1c: rerun the spike 1b email with a body written in Fo's own voice. Recipient: rfb5381@psu.edu only.

    backend/.venv/Scripts/python -u spikes/fo_send_voice_test.py              # dry run: prints instruction + body
    backend/.venv/Scripts/python -u spikes/fo_send_voice_test.py --yes-send   # real send (only after Het says yes)
    ... --tag SEALED-XXXXXXXX --watch 15                                      # later: watch for the reply via MCP

Measures:
  1. Does Fo show a confirmation preview, and does it contain our full body, subject and recipient?
     We answer "yes" only if it does (pre-send guard on Fo's preview). Otherwise we stop.
  2. Does the turn go "working", and does get_fo_reply polling work?
  3. Afterwards (--watch): does the reply show up through MCP, in this conversation (Fo CLAIMS replies
     "arrive back in this thread") or as a new email-channel conversation? Polls get_fo_conversation +
     list_fo_conversations every 30 s.
  4. Verbatim body + subject tag on the sent copy: run spikes/gmail_read.py --tag <TAG> (needs .env).
Log: spikes/logs/send_<tag>.json (gitignored).
"""

import argparse
import asyncio
import json
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
from sealed.fo.messages import new_tag, wrap_for_fo  # noqa: E402

RECIPIENT = "rfb5381@psu.edu"  # the ONLY allowed recipient
CC = "hetp2030@gmail.com"
ITEM = "one plain ceramic mug with a one-colour logo"
STOP_WORDS = ("payment", "purchase", "paid", "charged", "order placed", "checkout")
LOG_DIR = Path(__file__).resolve().parent / "logs"
log: list[dict] = []


def fo_voice_body() -> str:
    return (
        "Hello,\n\n"
        "I'm Fo, an AI assistant writing on behalf of Het Patel. Het is looking for a quote for a test item: "
        f"{ITEM}.\n\n"
        "Could you please reply with:\n"
        "- The total price, including any fees and delivery\n"
        "- The delivery date you can commit to\n"
        "- How long this price is valid\n\n"
        "Please use reply-all so Het sees your answer.\n\n"
        "Thank you,\n"
        "Fo, AI assistant to Het Patel"
    )


def guard_body(body: str) -> list[str]:
    """Spike-level pre-send rules: no digits, no currency, no money words we didn't intend."""
    problems = []
    if re.search(r"\d", body):
        problems.append("body contains a digit")
    if re.search(r"[$€£]|usd|dollar", body, re.I):
        problems.append("body contains a currency marker")
    if re.search(r"budget|ceiling|max(imum)? price|afford", body, re.I):
        problems.append("body mentions a budget")
    return problems


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def preview_check(text: str, subject: str, body: str, tag: str) -> dict:
    return {
        "has_recipient": RECIPIENT in text,
        "has_tag": tag in text,
        "has_exact_subject": subject in text,
        "has_full_body": squash(body) in squash(text),
        "no_digits_outside_ours": not re.search(r"\d", text.replace(tag, "").replace(subject, "")),
    }


def record(kind: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, **fields}
    log.append(entry)
    print(json.dumps(entry, indent=2, default=str), flush=True)


async def call(s: ClientSession, tool: str, args: dict) -> dict:
    record("request", tool=tool, args=args)
    t0 = time.perf_counter()
    r = await s.call_tool(tool, args, read_timeout_seconds=120)
    sc = r.structured_content or {}
    record("response", tool=tool, seconds=round(time.perf_counter() - t0, 1), raw=json.loads(r.model_dump_json(exclude_none=True)))
    if any(w in (sc.get("response") or "").lower() for w in STOP_WORDS):
        record("STOP_WARNING", note="Fo mentions payment/purchase/order. Stop and review.")
    return sc


async def turn(s: ClientSession, args: dict) -> dict:
    sc = await call(s, "ask_fo", args)
    t0, polls = time.perf_counter(), 0
    while sc.get("status") == "working" and time.perf_counter() - t0 < 600:
        polls += 1
        sc = await call(s, "get_fo_reply", {"conversationId": sc["conversationId"], "waitSeconds": 40})
    record("turn_done", status=sc.get("status"), polls=polls, went_working=polls > 0)
    return sc


async def session_ctx():
    storage = FileTokenStorage()
    refresh_if_needed(storage)
    token = storage.load()["tokens"]["access_token"]
    return create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})


async def send(tag: str, subject: str, body: str, instruction: str) -> None:
    async with await session_ctx() as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                sc = await turn(s, {"message": instruction, "newConversation": True})
                conv = sc.get("conversationId")
                record("conversation", conversationId=conv)
                text = sc.get("response") or ""
                if "?" in text:  # Fo is asking something: treat as a confirmation preview
                    chk = preview_check(text, subject, body, tag)
                    record("preview_check", **chk)
                    if all(chk.values()):
                        await turn(s, {"message": "Yes, send it exactly as written.", "conversationId": conv})
                    else:
                        record("NOT_CONFIRMED", note="Preview did not match our exact text; not answering yes.")
                r = await s.call_tool("get_fo_conversation", {"conversationId": conv})
                record("conversation_after_send", raw=r.structured_content)


async def watch(tag: str, minutes: float) -> None:
    logf = LOG_DIR / f"send_{tag}.json"
    conv = next(e["conversationId"] for e in json.loads(logf.read_text()) if e["kind"] == "conversation")
    async with await session_ctx() as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                before = await s.call_tool("list_fo_conversations", {})
                seen = {c["conversationId"] for c in (before.structured_content or {}).get("conversations", [])}
                base = len((await s.call_tool("get_fo_conversation", {"conversationId": conv})).structured_content.get("turns", []))
                deadline = time.time() + minutes * 60
                while time.time() < deadline:
                    turns = (await s.call_tool("get_fo_conversation", {"conversationId": conv})).structured_content.get("turns", [])
                    new_convs = [c for c in (await s.call_tool("list_fo_conversations", {})).structured_content.get(
                        "conversations", []) if c["conversationId"] not in seen]
                    if len(turns) > base or new_convs:
                        record("reply_via_mcp", new_turns=turns[base:], new_conversations=new_convs)
                        for c in new_convs:
                            r = await s.call_tool("get_fo_conversation", {"conversationId": c["conversationId"]})
                            record("new_conversation_transcript", raw=r.structured_content)
                        break
                    print(f"[watch] {time.strftime('%H:%M:%S')} nothing new via MCP", flush=True)
                    await asyncio.sleep(30)
                else:
                    record("no_reply_via_mcp", minutes=minutes)
    logf.write_text(json.dumps(json.loads(logf.read_text()) + log, indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-send", action="store_true", help="actually send (only after Het approves)")
    ap.add_argument("--tag", help="existing tag (for --watch)")
    ap.add_argument("--watch", type=float, help="minutes to watch for the reply via MCP")
    args = ap.parse_args()

    if args.watch:
        asyncio.run(watch(args.tag, args.watch))
        return

    tag = args.tag or new_tag()
    subject = f"Quote request: test item [{tag}]"
    body = fo_voice_body()
    instruction = wrap_for_fo(RECIPIENT, CC, subject, body)
    problems = guard_body(body)
    print("=== SUBJECT ===\n" + subject + "\n\n=== INSTRUCTION TO FO ===\n" + instruction)
    print("\n=== GUARD ===", problems or "passed")
    if problems:
        sys.exit("guard failed; not sending")
    if not args.yes_send:
        print("\nDry run only. Re-run with --yes-send after Het approves.")
        return
    record("built", tag=tag, to=RECIPIENT, cc=CC, subject=subject, email_body=body, fo_instruction=instruction)
    try:
        asyncio.run(send(tag, subject, body, instruction))
    finally:
        LOG_DIR.mkdir(exist_ok=True)
        (LOG_DIR / f"send_{tag}.json").write_text(json.dumps(log, indent=2, default=str))
        print(f"\nTAG={tag}")


if __name__ == "__main__":
    main()
