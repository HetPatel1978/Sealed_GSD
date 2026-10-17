"""Spike 1b: one real email through Fo (to the teammate only) + a concurrent harmless question.

    backend/.venv/Scripts/python -u spikes/fo_send_test.py

Logs every raw request/response to spikes/logs/send_<tag>.json (gitignored).
Never put a budget, ceiling or price in anything sent to Fo.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sealed.env import load_env  # noqa: E402

load_env()

from mcp import ClientSession  # noqa: E402
from mcp.client.auth import OAuthClientProvider  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402
from mcp.shared.auth import OAuthClientMetadata  # noqa: E402

from sealed.fo.auth import FO_URL, FileTokenStorage, refresh_if_needed, seconds_left  # noqa: E402
from sealed.fo.messages import build_quote_request  # noqa: E402

RECIPIENT = "rfb5381@psu.edu"  # the ONLY allowed recipient for this spike
CC = os.getenv("GMAIL_ADDRESS") or "hetp2030@gmail.com"
ITEM = "test item (one plain ceramic mug with a one-colour logo)"
QUESTION = "Hi Fo, quick question, no action needed: what is today's date?"
CONFIRM_WORDS = ("confirm", "good to send", "shall i", "should i send", "ready to send", "want me to send", "ok to send")
STOP_WORDS = ("payment", "purchase", "paid", "charged", "order placed", "checkout")
LOG_DIR = Path(__file__).resolve().parent / "logs"

log: list[dict] = []


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def record(lane: str, kind: str, **fields) -> None:
    entry = {"ts": now(), "lane": lane, "kind": kind, **fields}
    log.append(entry)
    print(json.dumps(entry, indent=2, default=str), flush=True)


async def call(session: ClientSession, lane: str, tool: str, args: dict) -> dict:
    record(lane, "request", tool=tool, args=args)
    t0 = time.perf_counter()
    result = await session.call_tool(tool, args, read_timeout_seconds=120)
    dt = round(time.perf_counter() - t0, 1)
    raw = json.loads(result.model_dump_json(exclude_none=True))
    record(lane, "response", tool=tool, seconds=dt, raw=raw)
    sc = result.structured_content or {}
    text = sc.get("response") or ""
    if any(w in text.lower() for w in STOP_WORDS):
        record(lane, "STOP_WARNING", note="Fo's response mentions payment/purchase/order. Review before continuing.")
    return sc


async def turn(session: ClientSession, lane: str, args: dict) -> dict:
    """ask_fo, then poll get_fo_reply while working. Returns the final structured result."""
    sc = await call(session, lane, "ask_fo", args)
    conv, polls, t0 = sc.get("conversationId"), 0, time.perf_counter()
    while sc.get("status") == "working" and time.perf_counter() - t0 < 600:
        polls += 1
        sc = await call(session, lane, "get_fo_reply", {"conversationId": conv, "waitSeconds": 40})
    record(lane, "turn_done", status=sc.get("status"), polls=polls, conversationId=conv)
    return sc


def looks_like_confirmation(text: str) -> bool:
    t = text.lower()
    return "?" in t and any(w in t for w in CONFIRM_WORDS)


async def send_lane(session: ClientSession, email) -> None:
    lane = "send"
    record(lane, "built", tag=email.tag, to=email.to, cc=email.cc, subject=email.subject,
           email_body=email.email_body, fo_instruction=email.fo_instruction)
    sc = await turn(session, lane, {"message": email.fo_instruction, "newConversation": True})
    text = sc.get("response") or ""
    if sc.get("status") == "complete" and looks_like_confirmation(text):
        echo_ok = email.tag in text and email.to in text
        record(lane, "confirmation_asked", echo_has_tag=email.tag in text, echo_has_recipient=email.to in text)
        if echo_ok:
            await turn(session, lane, {"message": "Yes, send it exactly as written.",
                                       "conversationId": sc["conversationId"]})
        else:
            record(lane, "confirmation_not_answered", note="Echo did not show our tag + recipient; not confirming.")


async def question_lane(session: ClientSession) -> None:
    await turn(session, "question", {"message": QUESTION, "newConversation": True})


async def no_browser(_url: str) -> None:
    raise RuntimeError("Token invalid and refresh failed: a browser sign-in would be needed. Aborting spike.")


async def main() -> None:
    storage = FileTokenStorage()
    left = seconds_left(storage)
    refreshed = refresh_if_needed(storage)
    record("auth", "token", seconds_left_before=round(left or 0), refreshed=refreshed,
           seconds_left_after=round(seconds_left(storage) or 0))

    email = build_quote_request(RECIPIENT, CC, ITEM)
    assert email.to == RECIPIENT

    auth = OAuthClientProvider(
        server_url=FO_URL,
        client_metadata=OAuthClientMetadata(client_name="Sealed (hackathon spike)",
                                            redirect_uris=["http://localhost:3030/callback"],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=storage, redirect_handler=no_browser,
    )
    t0 = time.perf_counter()
    try:
        async with create_mcp_http_client(auth=auth) as http_client:
            async with streamable_http_client(FO_URL, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    results = await asyncio.gather(send_lane(session, email), question_lane(session),
                                                   return_exceptions=True)
                    for name, r in zip(("send", "question"), results):
                        if isinstance(r, BaseException):
                            record(name, "exception", error=repr(r))
    finally:
        record("all", "elapsed", seconds=round(time.perf_counter() - t0, 1))
        LOG_DIR.mkdir(exist_ok=True)
        out = LOG_DIR / f"send_{email.tag}.json"
        out.write_text(json.dumps(log, indent=2, default=str))
        print(f"\nlog written: {out}\nTAG={email.tag}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
