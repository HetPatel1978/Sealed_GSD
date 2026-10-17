"""Spike 1c watcher: for N minutes, record every new thing that appears for one tag, in BOTH places:
  - Gmail (read-only IMAP): every message containing the tag, from anyone (dealer reply-all, Fo's CC copy,
    Fo's reports), with From/To/Cc/In-Reply-To and the cleaned text.
  - Fo MCP: new turns in the dealer's Fo conversation (Fo CLAIMS replies arrive there), and any new
    conversation (e.g. an email-channel one) with its transcript.

    backend/.venv/Scripts/python -u spikes/watch_replies.py --tag SEALED-XXXXXXXX --minutes 25

Log: spikes/logs/watch_<tag>.json (gitignored).
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sealed.env import load_env  # noqa: E402

load_env()

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402

from gmail_read import connect, describe, fetch, plain_text, search  # noqa: E402
from email_reply_parser import EmailReplyParser  # noqa: E402
from sealed.fo.auth import FO_URL, FileTokenStorage, refresh_if_needed  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"
log: list[dict] = []


def record(kind: str, **fields) -> None:
    e = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **fields}
    log.append(e)
    print(json.dumps(e, indent=2, default=str), flush=True)


async def main(tag: str, minutes: float, interval: int) -> None:
    send_log = json.loads((LOG_DIR / f"send_{tag}.json").read_text())
    conv = next(e["conversationId"] for e in send_log if e["kind"] == "conversation")
    imap = connect()
    seen_mail: set[str] = set()
    storage = FileTokenStorage()
    deadline = time.time() + minutes * 60
    refresh_if_needed(storage)
    token = storage.load()["tokens"]["access_token"]
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                first = await s.call_tool("list_fo_conversations", {})
                seen_convs = {c["conversationId"] for c in (first.structured_content or {}).get("conversations", [])}
                base = (await s.call_tool("get_fo_conversation", {"conversationId": conv})).structured_content
                n_turns = len(base.get("turns", []))
                record("baseline", dealer_conversation_turns=n_turns, known_conversations=len(seen_convs))
                while time.time() < deadline:
                    # Gmail
                    imap.noop()
                    for num in search(imap, f'"{tag}"'):
                        msg, thrid = fetch(imap, num)
                        mid = str(msg["Message-ID"])
                        if mid in seen_mail:
                            continue
                        seen_mail.add(mid)
                        raw = plain_text(msg)
                        record("gmail_message", headers=describe(msg, thrid),
                               clean_text=EmailReplyParser.parse_reply(raw).strip()[:1500], raw_chars=len(raw))
                    # Fo: dealer conversation
                    cur = (await s.call_tool("get_fo_conversation", {"conversationId": conv})).structured_content
                    turns = cur.get("turns", [])
                    if len(turns) != n_turns:
                        record("dealer_conversation_new_turns", new_turns=turns[n_turns:])
                        n_turns = len(turns)
                    # Fo: new conversations
                    lst = (await s.call_tool("list_fo_conversations", {})).structured_content.get("conversations", [])
                    for c in lst:
                        if c["conversationId"] not in seen_convs:
                            seen_convs.add(c["conversationId"])
                            t = await s.call_tool("get_fo_conversation", {"conversationId": c["conversationId"]})
                            record("new_fo_conversation", row=c, transcript=t.structured_content)
                    print(f"[watch] {time.strftime('%H:%M:%S')} checked", flush=True)
                    LOG_DIR.mkdir(exist_ok=True)
                    (LOG_DIR / f"watch_{tag}.json").write_text(json.dumps(log, indent=2, default=str))
                    await asyncio.sleep(interval)
    imap.logout()
    record("done", minutes=minutes)
    (LOG_DIR / f"watch_{tag}.json").write_text(json.dumps(log, indent=2, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--minutes", type=float, default=25)
    ap.add_argument("--interval", type=int, default=30)
    a = ap.parse_args()
    asyncio.run(main(a.tag, a.minutes, a.interval))
