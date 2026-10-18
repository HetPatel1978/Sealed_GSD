"""One research-only Fo discovery test. Re-runs poll/reuse, never resend the ask.

Run from the repository root with backend/.venv/Scripts/python.exe.
Raw results and conversation IDs stay in gitignored .sealed/.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sealed.env import load_env

load_env()

from sealed.fo.adapter import Store, _TURN_LOCK
from sealed.fo.client import FoTimeout, LiveFoClient
from sealed.service.lock import worker_lock

STATE = ROOT / ".sealed/fo_state.json"
TRACE = ROOT / ".sealed/fo_dealer_discovery_probe.json"
KEY = Store.key("fo-discovery-probe", "probe_01", "research_01")
INITIAL_CONVERSATION = None
QUESTION = """RESEARCH ONLY: Sealed dealer discovery capability test.
This is a software test, not a purchase request. Use only public web research.
Do not contact anyone: no emails, calls, texts, messages, form submissions,
bookings, reservations, purchases, payments or scheduled follow-ups. Do not use
my connected email, contacts or private past conversations for this research.
I am authorizing research and a response in THIS conversation only.

Find up to five authorized Honda franchise dealerships within 20 miles of US ZIP
19104 that are relevant to this exact request: a 2026 Honda Civic Sport sedan,
Crystal Black Pearl, automatic transmission. Today is October 18, 2026.
Do not relax the model/trim/color silently. A Honda dealership can be a candidate
without a matching listing, but label exact inventory as unknown in that case.

Read official dealer contact and inventory pages where possible. Return only
published business contacts, never guessed email patterns. Prefer a public sales
email; if none is published return null and include a published business phone.
Give official page URLs and short verbatim evidence for every email, phone,
street address and claimed matching vehicle listing. A contact form is not an email.
Describe distance as approximate unless you actually measured it, and say how
you estimated it; do not claim a strict 20-mile check if you cannot perform one.
List availability as 'listed online, unconfirmed' only with an exact matching
listing URL; otherwise 'unknown'. Do not claim anyone offered a price or confirmed
stock. Rank by evidence of specification match, proximity and usable contacts.
Do not ask for budget or use any remembered budget. No price recommendation is needed.

Return a JSON object with keys researched_at, dealers, limitations. Each dealer:
name, website, address, email (or null), phone (or null), distance_miles (or null),
distance_method, inventory_status, matching_listing_url (or null), match_notes,
sources [{url, supports, evidence}]. If web browsing is unavailable, say that
explicitly and return an empty dealers array; do not invent or use remembered data.
Finish the research in this turn. Do not offer or initiate outreach.
"""


def save(trace):
    TRACE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=TRACE.name + ".", suffix=".tmp", dir=TRACE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(trace, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, TRACE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    digest = hashlib.sha256(QUESTION.encode()).hexdigest()
    with worker_lock(STATE.with_suffix(".json.worker.lock")), _TURN_LOCK:
        state = Store(STATE)
        if state.data.get("active") not in (None, KEY):
            print(json.dumps({"status": "blocked", "reason": "existing_fo_turn_requires_review"}), flush=True)
            return 1
        trace = json.loads(TRACE.read_text(encoding="utf-8")) if TRACE.exists() else {
            "request": QUESTION, "request_sha256": digest, "attempted": False, "turns": []}
        if trace["request_sha256"] != digest:
            raise ValueError("Recorded discovery request differs; not issuing another request.")
        if trace.get("status") in {"complete", "failed"}:
            print(json.dumps({"status": trace["status"], "reused": True, "trace": str(TRACE)}), flush=True)
            return 0 if trace["status"] == "complete" else 1
        save(trace)
        client = LiveFoClient()
        try:
            if not trace["attempted"]:
                # Read-only auth/connectivity check before reserving the one ask.
                client.conversations()
                if "error_type" in trace:
                    trace["preflight_error_type"] = trace.pop("error_type")
                trace.update(attempted=True, attempted_at=datetime.now(timezone.utc).isoformat(), status="pending")
                save(trace)
                state.set_status(KEY, "pending", active=True, channel="research", research_only=True,
                                 fingerprint=digest, attempted_at=trace["attempted_at"])
                print(json.dumps({"status": "research_requested", "ask_count": 1}), flush=True)
                try:
                    result = client.ask(QUESTION, INITIAL_CONVERSATION)
                except FoTimeout:
                    result = {"status": "working"}
            else:
                result = client.reply(trace.get("conversation_id"), wait_seconds=20)
            deadline = time.monotonic() + 600
            while True:
                cid = result.get("conversationId")
                if cid:
                    if trace.get("conversation_id") not in (None, cid):
                        raise ValueError("Research conversation changed; stopped without another ask.")
                    trace["conversation_id"] = cid
                trace["turns"].append(result)
                trace["status"] = result.get("status", "unknown")
                save(trace)
                print(json.dumps({"status": trace["status"], "polls": len(trace["turns"]) - 1}), flush=True)
                if trace["status"] in {"complete", "failed"}:
                    trace["response"] = result.get("response", "")
                    trace["finished_at"] = datetime.now(timezone.utc).isoformat()
                    save(trace)
                    state.set_status(KEY, "researched" if trace["status"] == "complete" else "research_failed",
                                     active=False, conversation_id=trace.get("conversation_id"))
                    print(json.dumps({"status": trace["status"], "trace": str(TRACE), "outreach_authorized": False}), flush=True)
                    return 0 if trace["status"] == "complete" else 1
                if trace["status"] != "working" or time.monotonic() >= deadline:
                    return 1  # leave the reservation for polling/review, never repeat ask_fo
                try:
                    result = client.reply(trace.get("conversation_id"), wait_seconds=20)
                except FoTimeout:
                    result = {"status": "working"}
        except Exception as error:
            trace["error_type"] = type(error).__name__
            save(trace)
            print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                              "ask_attempted": trace["attempted"], "trace": str(TRACE)}), flush=True)
            return 1
        finally:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inline-existing", action="store_true", help="Retrieve the completed report inline, without repeating research.")
    args = parser.parse_args()
    if args.inline_existing:
        original = json.loads(TRACE.read_text(encoding="utf-8"))
        if original.get("status") != "complete" or not original.get("conversation_id"):
            raise SystemExit("Complete the original research first; no new ask was issued.")
        INITIAL_CONVERSATION = original["conversation_id"]
        TRACE = ROOT / ".sealed/fo_dealer_discovery_inline.json"
        KEY = Store.key("fo-discovery-probe", "probe_01", "inline_report_01")
        QUESTION = """Please retrieve the dealer research you ALREADY completed in this conversation.
The MCP response did not expose your attached document or any attachment URL.
Paste the existing results INLINE as a compact JSON object in your reply, under
5000 characters. No attachments, new documents, publishing or links in place of
the JSON. Do not repeat the web research. Do not contact anyone, email, call,
submit forms, book, pay, schedule tasks, or access my calendar, inbox or contacts.
If the existing report is unavailable, say that instead of inventing details.
JSON: {dealers:[{name,website,address,email,phone,distance_miles,distance_method,
inventory_status,matching_listing_url,sources:[official source URLs]}],limitations:[]}.
Only copy verified contacts and URLs from your existing report; use null for
missing information. Keep stock listed online and unconfirmed. Include no prices,
personal data, budget, or follow-up offer. Output the JSON itself as the reply."""
    raise SystemExit(main())
