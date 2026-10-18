"""T1: read-only Fo checks. No ask_fo, no sends.

    .venv/bin/python -u spikes/fo_introspect.py

Lists conversations, reads every email/voice conversation, lists resources/templates/prompts,
fetches OAuth metadata. Raw output goes to spikes/logs/introspect.json (gitignored: contains mail text).
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sealed.env import load_env  # noqa: E402

load_env()

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402

from sealed.fo.auth import FO_URL, FileTokenStorage, refresh_if_needed  # noqa: E402

LOG = Path(__file__).resolve().parent / "logs" / "introspect.json"
READ_CHANNELS = {"email", "voice", "sms"}


def dump(obj) -> object:
    return json.loads(obj.model_dump_json(exclude_none=True)) if hasattr(obj, "model_dump_json") else obj


async def safe(label: str, coro):
    try:
        return dump(await coro)
    except Exception as e:  # record, don't crash: some servers don't implement every list method
        return {"error": f"{type(e).__name__}: {e}"}


async def main() -> None:
    storage = FileTokenStorage()
    print(f"[token] refreshed: {refresh_if_needed(storage)}")
    token = storage.load()["tokens"]["access_token"]
    out: dict = {"oauth": {}}

    for path in ("/.well-known/oauth-protected-resource/fo", "/.well-known/oauth-protected-resource",
                 "/.well-known/oauth-authorization-server"):
        r = httpx.get("https://mcp.api.wajo.ai" + path, timeout=15)
        out["oauth"][path] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:300]}

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as s:
                out["initialize"] = dump(await s.initialize())
                out["resources_list"] = await safe("resources", s.list_resources())
                out["resource_templates_list"] = await safe("templates", s.list_resource_templates())
                out["prompts_list"] = await safe("prompts", s.list_prompts())

                res = await s.call_tool("list_fo_conversations", {})
                convs = (res.structured_content or {}).get("conversations", [])
                out["conversations"] = convs
                out["transcripts"] = {}
                for c in convs:
                    if c.get("channel") in READ_CHANNELS:
                        r = await s.call_tool("get_fo_conversation", {"conversationId": c["conversationId"]})
                        out["transcripts"][c["conversationId"]] = dump(r)

    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(out, indent=2, default=str))

    # Short, human summary (no ids)
    print("\n[oauth]")
    for p, v in out["oauth"].items():
        print(f"  {p}: {v['status']}")
    print("\n[resources]", json.dumps(out["resources_list"])[:600])
    print("[templates]", json.dumps(out["resource_templates_list"])[:600])
    print("[prompts]", json.dumps(out["prompts_list"])[:600])
    print("\n[conversations]")
    for c in convs:
        print(f"  {c.get('startedAt', '?')}  {c['channel']:<6}  {c['title']}")
    print("\n[email/voice transcripts]")
    for cid, t in out["transcripts"].items():
        sc = t.get("structured_content") or {}
        conv, turns = sc.get("conversation", {}), sc.get("turns", [])
        print(f"\n  -- {conv.get('channel')} | {conv.get('title')} | turns: {len(turns)} | is_error: {t.get('is_error')}")
        if not sc:
            print("    raw content:", json.dumps(t.get("content"))[:400])
        for turn in turns:
            print(f"    [{turn.get('role')}] {turn.get('at', '')} fields={sorted(turn)}")
            print("      " + turn.get("text", "")[:500].replace("\n", "\n      "))
    print(f"\nraw: {LOG}")


if __name__ == "__main__":
    asyncio.run(main())
