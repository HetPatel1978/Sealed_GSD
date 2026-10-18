"""Exercise cached Gmail handoff in an isolated controlled-test run. Zero network.

The real source inbox remains pending. Only a copied staging item is consumed;
any follow-up stays in a separate durable outbox. No ceiling/qualifier exists for
this sample, and this command cannot dispatch, start timers or request a model.
"""

import argparse
import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter

from sealed.core.engine import Engine
from sealed.core.events import EventLog
from sealed.core.models import Dealer, Job
from sealed.env import REPO_ROOT, load_env
from sealed.ingest.deliver import DeliveryError, DeliveryStore, QueueTransport, ReplyDelivery
from sealed.ingest.mail import Binding, MailMessage, match_message
from sealed.ingest.store import InboxStore
from sealed.parser.quote import DEFAULT_MODEL, ParserError
from sealed.parser.store import ParseStore


class ControlledSampleChecker:
    def contains_secret(self, text):
        return False  # this explicitly scoped test has no private ceiling


def no_qualification(*args):
    raise DeliveryError("sample_has_no_purchase_authorization")


def main(argv=None):
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT / ".sealed")
    args = parser.parse_args(argv)
    root = args.root
    destination = root / "handoff_smoke"
    account = os.getenv("GMAIL_ADDRESS", "").casefold()
    try:
        bindings = [Binding(**b) for b in json.loads((root / "mail_bindings.json").read_text(encoding="utf-8"))
            if (b["job_id"], b["run_id"]) == ("fo-adapter-smoke", "smoke_01")]
        successful_counter = json.loads((root / "bargain_revision_03.json").read_text(encoding="utf-8"))
        if not bindings or successful_counter["public_target"] != 90 or not successful_counter["send"]["audit"]["state"] == "passed":
            raise DeliveryError("controlled_test_evidence_required")
        with InboxStore(root / "inbox.sqlite3") as source:
            candidates = [r for r in source.pending(account) if r["match"].get("binding") and (
                r["match"]["binding"]["job_id"], r["match"]["binding"]["run_id"]) == ("fo-adapter-smoke", "smoke_01")]
            if not candidates:
                raise DeliveryError("no_staged_sample_reply")
            record = max(candidates, key=lambda r: (r["message"]["received_at"], int(r["message"]["gmail_id"])))
            mail = TypeAdapter(MailMessage).validate_json(json.dumps(record["message"]), strict=True)
            matched = match_message(mail, bindings, account)
            if matched.status != "ready":
                raise DeliveryError("sample_identity_needs_review")
            before = source.get(account, mail.gmail_id)
            with InboxStore(destination / "inbox.sqlite3") as inbox, ParseStore(root / "quote_parses.sqlite3") as cache, \
                    DeliveryStore(destination / "engine.sqlite3") as store:
                inbox.stage(account, mail, matched)
                checker = ControlledSampleChecker()
                try:
                    store.load(account, "fo-adapter-smoke", "smoke_01", checker=checker, qualifier=no_qualification)
                except DeliveryError as error:
                    if str(error) != "run_not_registered":
                        raise
                    job = Job("fo-adapter-smoke", "Sealed integration test", "sample quote for the integration test",
                        "not applicable (controlled test)", "the account owner", "for this test only", "2026-10-25", 90,
                        (Dealer(matched.binding.dealer_id, "integration test teammate", matched.binding.sender, mode="live"),),
                        run_id="smoke_01", mode="live")
                    # Import public scope only; do not replay RFQs, counters or claim past sends as engine events.
                    t0 = datetime.fromisoformat(matched.binding.sent_at)
                    eng = Engine(job, QueueTransport(), checker, no_qualification, cc=account,
                        tag=matched.binding.tag[1:-1], log=EventLog(job.id, job.run_id, job.mode, t0))
                    eng.create(0)
                    eng.state, eng.round = "RUNNING", 2
                    eng.contacted.add(matched.binding.dealer_id)
                    store.register(account, eng)
                delivery = ReplyDelivery(store, inbox, cache, account=account, bindings=bindings,
                    model=os.getenv("ANTHROPIC_QUOTE_MODEL") or DEFAULT_MODEL, checker=checker, qualifier=no_qualification)
                first = delivery.deliver(mail.gmail_id, job_id="fo-adapter-smoke", run_id="smoke_01", now=datetime.now(timezone.utc))
                second = delivery.deliver(mail.gmail_id, job_id="fo-adapter-smoke", run_id="smoke_01", now=datetime.now(timezone.utc))
                restored = store.load(account, "fo-adapter-smoke", "smoke_01", checker=checker, qualifier=no_qualification)
                report = {"mode": "controlled_cached_handoff", "source": mail.evidence_ref,
                    "first_delivery": asdict(first), "repeat_delivery": asdict(second),
                    "quotes": [{"price_total": q.price_total, "firm": q.firm, "meets_spec": q.meets_spec} for q in restored.ledger.quotes],
                    "pending_outreach": [{"message_id": msg.message_id, "kind": msg.kind,
                        "reply_to_message_id": msg.reply_to_message_id, "reply_thread_id": msg.reply_thread_id}
                        for msg in store.outbox(account, "fo-adapter-smoke", "smoke_01")],
                    "engine_state": restored.state, "event_count": len(restored.log.events),
                    "source_inbox_unchanged": source.get(account, mail.gmail_id) == before,
                    "model_requests": 0, "network_requests": 0, "messages_sent": 0,
                    "private_ceiling_configured": False}
                report_path = destination / ("acceptance_replay.json" if (destination / "acceptance.json").exists() else "acceptance.json")
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(json.dumps(report))
                return 0 if first.status == "applied" and second.duplicate and report["source_inbox_unchanged"] else 1
    except (DeliveryError, ParserError) as error:
        print(json.dumps({"error": str(error), "messages_sent": 0, "model_requests": 0}))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print(json.dumps({"error": "controlled_handoff_unavailable", "messages_sent": 0, "model_requests": 0}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
