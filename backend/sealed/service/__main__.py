"""Run the live account coordinator, or a fully offline controlled demonstration."""

import argparse
import getpass
import json
import os
import sqlite3
import sys
from contextlib import ExitStack
from dataclasses import fields
from pathlib import Path
from threading import Event

from pydantic import TypeAdapter

from sealed.core.models import Job
from sealed.env import REPO_ROOT, load_env
from sealed.fo.messages import new_tag
from sealed.ingest.deliver import DeliveryError, DeliveryStore
from sealed.ingest.gmail import GmailError, GmailReader
from sealed.ingest.mail import Binding
from sealed.ingest.store import InboxStore
from sealed.parser.quote import DEFAULT_MODEL, ParserError, QuoteParser
from sealed.parser.store import ParseStore
from sealed.private import vault
from sealed.private.runtime import for_run
from sealed.service.coordinator import Coordinator, Policy
from sealed.service.live import LiveTransport
from sealed.service.lock import worker_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim-demo", action="store_true", help="Run a complete offline demo with no live integrations.")
    parser.add_argument("--job", type=Path, help="Register a public Job JSON; it must have mode=live and no private fields.")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / ".sealed/engine.sqlite3")
    parser.add_argument("--inbox", type=Path, default=REPO_ROOT / ".sealed/inbox.sqlite3")
    parser.add_argument("--cache", type=Path, default=REPO_ROOT / ".sealed/quote_parses.sqlite3")
    parser.add_argument("--bindings", type=Path, default=REPO_ROOT / ".sealed/mail_bindings.json")
    parser.add_argument("--fo-state", type=Path, default=REPO_ROOT / ".sealed/fo_state.json")
    parser.add_argument("--audits", type=Path, default=REPO_ROOT / ".sealed/audits.sqlite3")
    parser.add_argument("--restore-private", action="store_true", help="Prompt privately for each run's ceiling; never persist it.")
    parser.add_argument("--allow-model-requests", action="store_true", help="Authorize sharing selected runs' seller replies with Anthropic.")
    parser.add_argument("--allow-dispatch", action="store_true", help="Authorize guarded email outreach for selected registered runs.")
    parser.add_argument("--allow-calls", action="store_true", help="Also authorize configured phone calls.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--job-id")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    if args.sim_demo:
        from sealed.private.runtime import run_coordinator_demo
        report = run_coordinator_demo()
        output = REPO_ROOT / ".sealed/coordinator_demo.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    if not 0.1 <= args.interval <= 60 or bool(args.job_id) != bool(args.run_id):
        parser.error("Use an interval from 0.1 to 60 seconds; provide both --job-id and --run-id.")
    if (args.allow_model_requests or args.allow_dispatch or args.allow_calls) and not args.job and not args.job_id:
        parser.error("Choose one run with --job or --job-id and --run-id before enabling external actions.")
    load_env()
    account = os.getenv("GMAIL_ADDRESS", "").casefold()
    restored = set()
    stop = Event()
    try:
        with worker_lock(args.fo_state.with_suffix(args.fo_state.suffix + ".worker.lock")), ExitStack() as stack:
            store = stack.enter_context(DeliveryStore(args.db))
            inbox = stack.enter_context(InboxStore(args.inbox))
            cache = stack.enter_context(ParseStore(args.cache))
            model = os.getenv("ANTHROPIC_QUOTE_MODEL") or DEFAULT_MODEL
            extractor = stack.enter_context(QuoteParser(os.getenv("ANTHROPIC_API_KEY", ""), model)) if args.allow_model_requests else None
            bindings = [Binding(**item) for item in json.loads(args.bindings.read_text(encoding="utf-8"))] if args.bindings.exists() else []
            policy = Policy(args.allow_model_requests, args.allow_dispatch, args.allow_calls)
            def coordinator(reader):
                return Coordinator(store, inbox, cache, account=account, reader=reader, extractor=extractor,
                    runtime=for_run, transport_factory=lambda engine: LiveTransport(reader, engine, state=args.fo_state, audits=args.audits),
                    bindings=bindings, policy=policy, model=model)
            # Registration needs no external client or private ceiling.
            class AccountOnly:
                address = account
            setup = coordinator(AccountOnly())
            if args.job:
                data = json.loads(args.job.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or set(data) - {f.name for f in fields(Job)}:
                    raise DeliveryError("public_job_fields_only")
                job = TypeAdapter(Job).validate_json(json.dumps(data), strict=True)
                if job.mode != "live":
                    raise DeliveryError("live_job_mode_required")
                if args.job_id and (args.job_id, args.run_id) != (job.id, job.run_id):
                    raise DeliveryError("job_selection_conflict")
                if (job.id, job.run_id) in setup.scopes():
                    existing = setup._load((job.id, job.run_id), for_run(job.id, job.run_id))
                    if existing.job != job:
                        raise DeliveryError("registered_job_conflict")
                else:
                    setup.register(job, cc=account, tag=new_tag())
            selected = [(args.job_id, args.run_id)] if args.job_id else (
                [(job.id, job.run_id)] if args.job else setup.scopes())
            if not selected:
                raise DeliveryError("no_registered_jobs")
            for scope in selected:
                engine = store.load(account, *scope, checker=for_run(*scope).checker, qualifier=for_run(*scope).qualifier)
                if engine.job.mode != "live":
                    raise DeliveryError("live_job_mode_required")
            if args.restore_private:
                if not sys.stdin.isatty():
                    raise DeliveryError("interactive_terminal_required_for_private_restore")
                for job_id, _ in selected:
                    if job_id not in restored:
                        value = getpass.getpass("Restore this job's private ceiling (input hidden): ")
                        vault.store(job_id, float(value))
                        restored.add(job_id)
                        del value
            while not stop.is_set():
                try:
                    # Reconnect per cycle; a stale IMAP session cannot authorize sending.
                    with GmailReader(account, os.getenv("GMAIL_APP_PASSWORD", "")) as reader:
                        report = coordinator(reader).tick(scopes=selected)
                except (GmailError, OSError, TimeoutError):
                    report = {"status": "mail_poll_failed", "runs": [], "dispatched": 0}
                print(json.dumps(report), flush=True)
                if args.once:
                    return 0 if report["status"] == "ok" else 1
                stop.wait(args.interval)
    except KeyboardInterrupt:
        stop.set()
        print(json.dumps({"status": "stopped"}))
        return 0
    except (DeliveryError, ParserError) as error:
        print(json.dumps({"error": str(error)}))
    except RuntimeError:
        print(json.dumps({"error": "coordinator_already_running_or_runtime_unavailable"}))
    except (ValueError, OSError, KeyError, TypeError, EOFError, sqlite3.Error):
        print(json.dumps({"error": "coordinator_unavailable"}))
    finally:
        for job_id in restored:
            vault.forget(job_id)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
