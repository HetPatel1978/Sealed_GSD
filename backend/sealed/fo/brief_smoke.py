"""Controlled brief-flow acceptance. Dry run unless --yes-send is explicit.

    python -m sealed.fo.brief_smoke --to consenting-teammate@example.org

This test has no real job, buying intent, target or ceiling. It requests one sample
written quote. Sending requires review of the printed recipient and brief; this
command never releases an existing account pause or invents a retry run ID.
"""

import argparse
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

from sealed.core.guard import Guard
from sealed.core.models import EmailBrief, Outbound, outbound_fingerprint
from sealed.env import REPO_ROOT, load_env
from sealed.fo import messages
from sealed.fo.smoke import NoCeiling, build_message as legacy_message
from sealed.ingest.audit import AuditRequest, AuditStore, GmailAuditor, reply_binding
from sealed.ingest.gmail import GmailError, GmailReader


def build_message(to: str, cc: str, run_id: str) -> Outbound:
    # Retain the legacy helper's strict address/run validation, not its payload.
    base = legacy_message(to, cc, run_id)
    brief = EmailBrief("the account owner", "sample vehicle for a controlled Sealed integration test",
                       "not applicable (controlled test)", "for this test only")
    subject = f"Sealed brief integration test [SEALED-BRIEF-{run_id}]"
    body = messages.render_email_brief("rfq", brief)
    return replace(base, job_id="fo-brief-smoke", subject=subject, body=body, email_brief=brief,
                   fo_instruction=messages.wrap_email_brief_for_fo(to, cc, subject, body))


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True)
    parser.add_argument("--cc", default=os.getenv("GMAIL_ADDRESS"))
    parser.add_argument("--run-id", default="brief_01")
    parser.add_argument("--state", type=Path, default=REPO_ROOT / ".sealed" / "fo_state.json")
    parser.add_argument("--audits", type=Path, default=REPO_ROOT / ".sealed" / "audits.sqlite3")
    parser.add_argument("--binding", type=Path, default=None)
    parser.add_argument("--audit-seconds", type=int, default=30)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--yes-send", action="store_true", help="send once after explicit approval of this test")
    mode.add_argument("--audit-only", action="store_true", help="recheck Gmail for this recorded attempt; never ask Fo")
    args = parser.parse_args(argv)
    try:
        msg = build_message(args.to, args.cc, args.run_id)
    except ValueError as error:
        parser.error(str(error))
    if not 0 <= args.audit_seconds <= 60:
        parser.error("Audit wait must be between zero and sixty seconds.")
    guard = Guard(NoCeiling())
    if not guard.check(msg).passed:
        parser.error("Controlled test brief failed the guard; nothing was sent.")
    print(f"To: {msg.to}\nCC: {msg.cc}\nSubject: {msg.subject}\n\n{msg.body}")
    if not args.yes_send and not args.audit_only:
        print("\nDry run: no Gmail or Fo connection, no state changed. Review before --yes-send.")
        return 0

    from sealed.fo.adapter import FoTransport, Store
    state = Store(args.state)
    record = state.message(Store.key(msg.job_id, msg.run_id, msg.message_id))
    if args.yes_send and state.data.get("active"):
        print("An earlier account turn requires review. Nothing was sent; the pause was preserved.")
        return 1
    if args.audit_only and (not record or record.get("fingerprint") != outbound_fingerprint(msg)
                            or not record.get("attempted_at")):
        print("No matching timestamped attempt exists. Nothing was sent or changed.")
        return 1
    try:
        # Establish read-only audit access before any non-idempotent Fo action.
        with GmailReader(os.getenv("GMAIL_ADDRESS", ""), os.getenv("GMAIL_APP_PASSWORD", "")) as reader:
            if msg.cc.lower() != reader.address:
                print("The CC must be the configured Gmail audit account. Nothing was sent.")
                return 1
            with AuditStore(args.audits) as store:
                auditor = GmailAuditor(reader, guard, store)
                senders = ("fo@wajo.ai", reader.address)
                if args.audit_only:
                    evidence = (record.get("audit") or {}).get("evidence") or {}
                    request = AuditRequest(msg, record["attempted_at"], senders,
                                           sent_message_id=evidence.get("message_id", ""))
                    audit = auditor.wait(request, seconds=args.audit_seconds)
                    summary = {"audit_state": audit.state, "reason_code": audit.reason_code,
                               "account_pause_preserved": bool(state.data.get("active")), "sent_now": False}
                else:
                    from sealed.fo.client import LiveFoClient
                    def check_sent(outbound, attempted_at):
                        return auditor.wait(AuditRequest(outbound, attempted_at, senders), seconds=args.audit_seconds)
                    client = LiveFoClient()
                    try:
                        sent = FoTransport(client, args.state, sent_auditor=check_sent).send(
                            0, msg, lambda preview: guard.check(msg, preview=preview).passed)
                    finally:
                        client.close()
                    audit = sent.audit
                    summary = {"sent": sent.sent, "reason_code": sent.reason_code,
                               "audit_state": audit.state if audit else None, "errors": sent.errors}
                    record = Store(args.state).message(Store.key(msg.job_id, msg.run_id, msg.message_id))
                    request = AuditRequest(msg, record["attempted_at"], senders) if record and record.get("attempted_at") else None
                if audit is not None and audit.matched and request is not None:
                    path = args.binding or REPO_ROOT / ".sealed" / f"brief_binding_{args.run_id}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps([asdict(reply_binding(request, audit))], indent=2), encoding="utf-8")
                print(json.dumps(summary))
                return 0 if audit is not None and audit.matched and not summary.get("reason_code") else 1
    except (GmailError, OSError):
        print("Gmail auditing or local storage failed. Check the recorded attempt; do not resend.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
