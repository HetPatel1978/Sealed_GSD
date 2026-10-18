"""Read-only Gmail quote preview. No sends, inbox acknowledgments or engine actions.

Run from backend: python -m sealed.ingest.parse --job-id JOB --run-id RUN --spec "exact specification"
The configured Claude API receives only reply text, specification, currency and
Gmail receipt time. Model requests/results are cached locally, never Fo memory.
"""

import argparse
import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sealed.core.ledger import Ledger
from sealed.core.models import Inbound
from sealed.env import REPO_ROOT, load_env
from sealed.ingest.gmail import GmailError, GmailReader
from sealed.ingest.mail import Binding, match_message
from sealed.ingest.store import InboxStore
from sealed.parser.quote import Context, DEFAULT_MODEL, ParserError, QuoteParser, input_digest
from sealed.parser.store import ParseStore


def collect_latest(reader, bindings, inbox):
    """Select the latest associated message per scope, including review-only updates.

    A newer unauthenticated/ambiguous update stops this preview; it never silently
    falls back to an older ready offer. Fetching remains bounded by GmailReader.
    """
    if not bindings or len({(b.job_id, b.run_id) for b in bindings}) != 1:
        raise ParserError("one_job_run_required")
    uids = set()
    for sender in sorted({b.sender for b in bindings}):
        since = min(datetime.fromisoformat(b.sent_at) for b in bindings if b.sender == sender)
        uids.update(reader.search(f"from:{sender} after:{int((since - timedelta(days=1)).timestamp())}"))
    if len(uids) > 1000:
        raise GmailError("Too many reply candidates; narrow the bindings.")
    latest = {}
    for uid in sorted(uids, key=int):
        mail = reader.fetch(uid)
        match = match_message(mail, bindings, reader.address)
        if match.status == "ignored":
            continue
        inbox.stage(reader.address, mail, match)
        if match.binding is None:
            # An ambiguous scope cannot be safely assigned or ignored as stale.
            raise ParserError("latest_reply_needs_identity_review")
        key = match.binding.scope
        prior = latest.get(key)
        if prior is None or (datetime.fromisoformat(mail.received_at), int(mail.uid)) > (
                datetime.fromisoformat(prior[0].received_at), int(prior[0].uid)):
            latest[key] = (mail, match)
    if not latest:
        raise ParserError("no_associated_seller_reply")
    return latest


def prepare_inputs(reader, bindings, inbox, *, specification, currency, now):
    Context(specification, currency, now.isoformat(), now.date().isoformat())
    latest = collect_latest(reader, bindings, inbox)
    inputs = []
    for scope, (mail, match) in sorted(latest.items()):
        item = {"job_id": scope[0], "run_id": scope[1], "dealer_id": scope[2],
            "evidence_ref": mail.evidence_ref, "status": match.status}
        if match.status == "ready":
            item["model_input"] = {"requested_specification": specification, "expected_currency": currency,
                "received_at": mail.received_at, "seller_reply": mail.clean_text}
        inputs.append(item)
    return {"inputs": inputs, "model_requests": 0, "messages_sent": 0, "inbox_acknowledged": False}


def preview(reader, bindings, inbox, cache, extractor, *, specification, currency, now, approved_input=None):
    # Validate all public context before making any model request.
    Context(specification, currency, now.isoformat(), now.date().isoformat())
    latest = collect_latest(reader, bindings, inbox)
    if approved_input is not None:
        if not isinstance(approved_input, dict):
            raise ParserError("invalid_prepared_input")
        actual = []
        for scope, (mail, match) in sorted(latest.items()):
            item = {"job_id": scope[0], "run_id": scope[1], "dealer_id": scope[2],
                "evidence_ref": mail.evidence_ref, "status": match.status}
            if match.status == "ready":
                item["model_input"] = {"requested_specification": specification, "expected_currency": currency,
                    "received_at": mail.received_at, "seller_reply": mail.clean_text}
            actual.append(item)
        if actual != approved_input.get("inputs"):
            raise ParserError("prepared_input_changed")
    reports = []
    for scope, (mail, match) in sorted(latest.items()):
        if match.status != "ready":
            reports.append({"job_id": scope[0], "run_id": scope[1], "dealer_id": scope[2],
                "evidence_ref": mail.evidence_ref, "status": "review", "reason": "latest_reply_needs_identity_review"})
            continue
        context = Context(specification, currency, mail.received_at, now.date().isoformat(), same_spec_allowed=True)
        digest = input_digest(mail.clean_text, context, extractor.model)
        result = cache.get(reader.address, mail.gmail_id, digest)
        cached = result is not None
        if result is None:
            result = extractor.parse(mail.clean_text, context)
            cache.put(reader.address, mail.gmail_id, digest, result)
        inbound = Inbound(scope[2], "email", mail.clean_text, result.parsed,
            in_reply_to=match.in_reply_to, from_addr=mail.from_addr)
        ledger = Ledger("9999-12-31", {scope[2]: match.binding.sender}, currency)
        quote = ledger.add(mail.evidence_ref, inbound, datetime.fromisoformat(mail.received_at).timestamp())
        reports.append({"job_id": scope[0], "run_id": scope[1], "dealer_id": scope[2],
            "evidence_ref": mail.evidence_ref, "status": "review" if result.review_reasons else "validated",
            "cached": cached, "parsed": asdict(result.parsed), "valid_until": result.valid_until,
            "review_reasons": list(result.review_reasons), "ledger_firm": quote.firm,
            "deadline_checked": False})
    return {"replies": reports, "model": extractor.model, "engine_delivery": False,
        "inbox_acknowledged": False, "messages_sent": 0}


def main(argv=None):
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--currency", choices=("USD", "CAD", "EUR", "GBP"), default="USD")
    parser.add_argument("--bindings", type=Path, default=REPO_ROOT / ".sealed/mail_bindings.json")
    parser.add_argument("--inbox", type=Path, default=REPO_ROOT / ".sealed/inbox.sqlite3")
    parser.add_argument("--cache", type=Path, default=REPO_ROOT / ".sealed/quote_parses.sqlite3")
    parser.add_argument("--prepare-only", action="store_true", help="Save exact candidate input locally; do not call Claude.")
    parser.add_argument("--approved-input", type=Path, help="Require the latest input to exactly match a prepared report.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        bindings = [Binding(**b) for b in json.loads(args.bindings.read_text(encoding="utf-8"))
            if (b["job_id"], b["run_id"]) == (args.job_id, args.run_id)]
        if not bindings:
            raise ParserError("no_trusted_bindings")
        with GmailReader(os.getenv("GMAIL_ADDRESS", ""), os.getenv("GMAIL_APP_PASSWORD", "")) as reader, \
                InboxStore(args.inbox) as inbox:
            if args.prepare_only:
                report = prepare_inputs(reader, bindings, inbox, specification=args.spec,
                    currency=args.currency, now=datetime.now(timezone.utc))
            else:
                with ParseStore(args.cache) as cache, QuoteParser(os.getenv("ANTHROPIC_API_KEY", ""),
                        os.getenv("ANTHROPIC_QUOTE_MODEL") or DEFAULT_MODEL) as extractor:
                    report = preview(reader, bindings, inbox, cache, extractor, specification=args.spec,
                        currency=args.currency, now=datetime.now(timezone.utc), approved_input=(
                            json.loads(args.approved_input.read_text(encoding="utf-8")) if args.approved_input else None))
        output = args.output or REPO_ROOT / (".sealed/quote_parse_input.json" if args.prepare_only else ".sealed/quote_parse_preview.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps(report, allow_nan=False))
        if args.prepare_only:
            return 0 if all(r["status"] == "ready" for r in report["inputs"]) else 1
        return 0 if all(r["status"] == "validated" for r in report["replies"]) else 1
    except ParserError as error:
        print(json.dumps({"error": error.code, "messages_sent": 0, "inbox_acknowledged": False}))
    except (GmailError, OSError, ValueError, TypeError, sqlite3.Error):
        print(json.dumps({"error": "quote_preview_unavailable", "messages_sent": 0, "inbox_acknowledged": False}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
