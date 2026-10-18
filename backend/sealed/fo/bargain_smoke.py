"""One controlled counter-reply using the real strategy and the reviewed $100 sample.

This is a fixture-specific demonstration, not the unfinished general quote parser.
It rereads Gmail, rejects changed/newer terms, validates the sample in the ledger,
advances the demo to round two and asks Fo to reply at the user's explicit target.
Dry run unless --yes-send is provided. No ceiling or purchase authorization exists.
"""

import argparse
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from sealed.core import strategy
from sealed.core.guard import Guard
from sealed.core.ledger import Ledger
from sealed.core.models import Dealer, Inbound, Job, ParsedReply
from sealed.env import REPO_ROOT, load_env
from sealed.fo import messages
from sealed.fo.smoke import NoCeiling
from sealed.ingest.audit import AuditRequest, AuditStore, GmailAuditor
from sealed.ingest.gmail import GmailError, GmailReader
from sealed.ingest.mail import Binding, MailMessage, match_message

SAMPLE = ("Hi Fo, This is a sample quote for the integration test. "
          "The total is $100.00 USD, including all fees and taxes. "
          "Availability is October 25, 2026. This quote is valid through October 31, 2026. Thanks!")


def latest_reply(reader: GmailReader, binding: Binding) -> MailMessage:
    since = int(datetime.fromisoformat(binding.sent_at).timestamp()) - 86400
    candidates = []
    for uid in reader.search(f"from:{binding.sender} after:{since}"):
        mail = reader.fetch(uid)
        if (binding.tag in mail.subject or mail.thread_id == binding.thread_id
                or binding.outbound_message_id in mail.references + mail.reply_to_ids):
            candidates.append(mail)
    if not candidates:
        raise ValueError("No associated seller reply exists; nothing was sent.")
    latest = max(candidates, key=lambda m: (datetime.fromisoformat(m.received_at), int(m.uid)))
    matched = match_message(latest, [binding], reader.address)
    if matched.status != "ready":
        raise ValueError("The latest associated reply needs review; nothing was sent.")
    return latest


def prepare(reply: MailMessage, binding: Binding, target: float, cc: str, *, today: str | None = None):
    if (binding.job_id, binding.run_id) != ("fo-adapter-smoke", "smoke_01"):
        raise ValueError("This command only supports the original controlled smoke test.")
    if not isfinite(target) or target <= 0:
        raise ValueError("The sample target must be finite and positive.")
    matched = match_message(reply, [binding], cc)
    if matched.status != "ready" or not reply.message_id or not reply.thread_id:
        raise ValueError("A verified, associated Gmail reply is required.")
    if " ".join(reply.clean_text.split()) != SAMPLE:
        raise ValueError("The latest reply differs from the reviewed sample; do not use stale $100 terms.")
    today = today or datetime.now(timezone.utc).date().isoformat()
    if today > "2026-10-31":
        raise ValueError("The sample quote expired; request updated terms before countering.")
    # The reviewed fixture supplies these fields. The ledger independently checks
    # sender, currency, positive literal total and qualification. No LLM is used.
    parsed = ParsedReply(100.0, True, available="2026-10-25", currency="USD")
    inbound = Inbound(binding.dealer_id, "email", reply.clean_text, parsed,
                      in_reply_to=matched.in_reply_to, from_addr=reply.from_addr)
    job = Job(binding.job_id, "Sealed integration test", "sample quote for the integration test",
              "not applicable (controlled test)", "the account owner", "for this test only",
              "2026-10-25", target, (Dealer(binding.dealer_id, "integration test teammate", binding.sender, mode="live"),),
              run_id=binding.run_id, mode="live")
    ledger = Ledger(job.deadline, {binding.dealer_id: binding.sender}, job.currency)
    quote = ledger.add(reply.evidence_ref, inbound, datetime.fromisoformat(reply.received_at).timestamp())
    decisions = strategy.round2(job, ledger)
    if len(decisions) != 1 or decisions[0].kind != "counter" or not quote.firm:
        raise ValueError("The strategy chose no counter: the target must be below the verified quote.")
    decision = decisions[0]
    msg = messages.build(job, decision, "counter_01", binding.tag[1:-1], cc)
    subject = reply.subject if reply.subject.lower().startswith("re:") else "Re: " + reply.subject
    msg = replace(msg, subject=subject, reply_to_message_id=reply.message_id, reply_thread_id=reply.thread_id,
                  fo_instruction=messages.wrap_email_brief_for_fo(msg.to, cc, subject, msg.body, reply.message_id))
    guard = Guard(NoCeiling())
    if not guard.check(msg).passed:
        raise ValueError("Counter brief failed its safety checks; nothing was sent.")
    trace = {"mode": "controlled_live_demo", "extraction": "reviewed_sample_fixture",
             "general_quote_parser_implemented": False, "round_advance": "user_requested_demo",
             "source": {"evidence_ref": reply.evidence_ref, "message_id": reply.message_id,
                        "thread_id": reply.thread_id, "authenticated": reply.authenticated},
             "quote": asdict(quote), "public_target": target, "decision": asdict(decision),
             "no_competing_quote_used": decision.cited_quote_id is None,
             "outbound_id": msg.message_id, "pre_send_guard_passed": True}
    return msg, guard, trace


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=float)
    parser.add_argument("--bindings", type=Path, default=REPO_ROOT / ".sealed" / "mail_bindings.json")
    parser.add_argument("--state", type=Path, default=REPO_ROOT / ".sealed" / "fo_state.json")
    parser.add_argument("--trace", type=Path, default=REPO_ROOT / ".sealed" / "bargain_smoke_01.json")
    parser.add_argument("--audits", type=Path, default=REPO_ROOT / ".sealed" / "audits.sqlite3")
    parser.add_argument("--yes-send", action="store_true")
    args = parser.parse_args(argv)
    try:
        bindings = [Binding(**b) for b in json.loads(args.bindings.read_text(encoding="utf-8"))]
        matching = [b for b in bindings if (b.job_id, b.run_id) == ("fo-adapter-smoke", "smoke_01")]
        if len(matching) != 1:
            raise ValueError("Exactly one original controlled-test binding is required.")
        binding = matching[0]
        with GmailReader(os.getenv("GMAIL_ADDRESS", ""), os.getenv("GMAIL_APP_PASSWORD", "")) as reader:
            reply = latest_reply(reader, binding)
            msg, guard, trace = prepare(reply, binding, args.target, reader.address)
            print(json.dumps({"quote": trace["quote"]["price_total"], "target": args.target,
                              "action": trace["decision"]["action"], "offer": msg.offer,
                              "to": msg.to, "cc": msg.cc, "subject": msg.subject, "reply_in_existing_thread": True}))
            if not args.yes_send:
                print("Dry run: Gmail was read-only; no Fo call or email was sent.")
                return 0
            from sealed.fo.adapter import FoTransport, Store
            from sealed.fo.client import LiveFoClient
            state = Store(args.state)
            if state.data.get("active"):
                raise ValueError("An account turn needs review; no new action was started.")
            args.trace.parent.mkdir(parents=True, exist_ok=True)
            args.trace.write_text(json.dumps(trace, indent=2), encoding="utf-8")
            with AuditStore(args.audits) as store:
                auditor = GmailAuditor(reader, guard, store)
                def check_sent(outbound, attempted_at):
                    return auditor.wait(AuditRequest(outbound, attempted_at, ("fo@wajo.ai", reader.address),
                                                     thread_id=reply.thread_id), seconds=30)
                client = LiveFoClient()
                try:
                    result = FoTransport(client, args.state, sent_auditor=check_sent).send(
                        0, msg, lambda preview: guard.check(msg, preview=preview).passed)
                finally:
                    client.close()
                trace["send"] = {"sent": result.sent, "reason_code": result.reason_code, "errors": result.errors,
                                 "draft": asdict(result.draft) if result.draft else None,
                                 "audit": asdict(result.audit) if result.audit else None}
                args.trace.write_text(json.dumps(trace, indent=2), encoding="utf-8")
                print(json.dumps({"sent": result.sent, "reason_code": result.reason_code,
                                  "audit_state": result.audit.state if result.audit else None, "errors": result.errors}))
                return 0 if result.sent and result.reason_code is None and result.audit and result.audit.matched else 1
    except ValueError as error:
        print(str(error))
        return 1
    except (GmailError, OSError):
        print("Mail evidence or local storage could not be read. Check the recorded attempt; do not resend.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
