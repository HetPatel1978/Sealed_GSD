"""T2: ask Fo about itself in ONE new conversation. No sends, no actions.

    backend/.venv/Scripts/python -u spikes/fo_ask_self.py

Exercises the get_fo_reply path if the turn goes "working". Log: spikes/logs/ask_self.json (gitignored).
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sealed.env import load_env  # noqa: E402

load_env()

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402

from sealed.fo.auth import FO_URL, FileTokenStorage, refresh_if_needed  # noqa: E402

QUESTION = (
    "Please don't take any action or contact anyone; I just have questions. "
    "What can you do for me? Which channels can you use to contact people? Can you call a business for me, "
    "and can I get a transcript or summary of the call afterwards? Do you stick to a script I give you on a "
    "call? Do you remember things across conversations? When do you ask me for confirmation? Can you ever "
    "make purchases or payments? Is there a webhook for new replies (conversation.reply)?"
)
LOG = Path(__file__).resolve().parent / "logs" / "ask_self.json"


async def main() -> None:
    storage = FileTokenStorage()
    refresh_if_needed(storage)
    token = storage.load()["tokens"]["access_token"]
    log = []
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                t0 = time.perf_counter()
                r = await s.call_tool("ask_fo", {"message": QUESTION, "newConversation": True}, read_timeout_seconds=120)
                sc = r.structured_content or {}
                log.append({"tool": "ask_fo", "s": round(time.perf_counter() - t0, 1), "result": sc})
                polls = 0
                while sc.get("status") == "working" and time.perf_counter() - t0 < 600:
                    polls += 1
                    t1 = time.perf_counter()
                    r = await s.call_tool("get_fo_reply", {"conversationId": sc["conversationId"], "waitSeconds": 40},
                                          read_timeout_seconds=120)
                    sc = r.structured_content or {}
                    log.append({"tool": "get_fo_reply", "s": round(time.perf_counter() - t1, 1), "result": sc})
                total = round(time.perf_counter() - t0, 1)
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(log, indent=2))
    for e in log:
        res = {k: v for k, v in e["result"].items() if k != "conversationId"}
        print(f"--- {e['tool']} ({e['s']}s): status={res.get('status')} progress={res.get('progress')!r}")
    print(f"\npolls: {polls}; total: {total}s\n\nFINAL RESPONSE:\n{sc.get('response')}")


if __name__ == "__main__":
    asyncio.run(main())
