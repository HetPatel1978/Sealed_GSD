"""Spike 2: read Gmail over IMAP (read-only), audit Fo's CC copy, wait for the seller's reply-all.

    .venv/bin/python -u spikes/gmail_read.py --tag SEALED-XXXXXXXX
    .venv/bin/python -u spikes/gmail_read.py --query "Quote request test"   # connectivity check

Credentials: GMAIL_ADDRESS, GMAIL_APP_PASSWORD from .env only. The mailbox is opened read-only.
--tag reads the expected subject/body from spikes/logs/send_<tag>.json (written by fo_send_test.py).
"""

import argparse
import difflib
import email
import imaplib
import json
import os
import re
import sys
import time
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from sealed.env import load_env  # noqa: E402

from email_reply_parser import EmailReplyParser  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"
FO_SENDER = "fo@wajo.ai"
SELLER = "rfb5381@psu.edu"


def connect() -> imaplib.IMAP4_SSL:
    load_env()
    user, pw = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pw:
        sys.exit("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing from .env")
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(user, pw.replace(" ", ""))
    m.select('"[Gmail]/All Mail"', readonly=True)
    return m


def search(m: imaplib.IMAP4_SSL, raw: str) -> list[bytes]:
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    typ, data = m.search(None, "X-GM-RAW", f'"{escaped}"')
    return data[0].split() if typ == "OK" and data and data[0] else []


def fetch(m: imaplib.IMAP4_SSL, num: bytes) -> tuple[EmailMessage, str]:
    typ, data = m.fetch(num, "(X-GM-THRID RFC822)")
    # Gmail can interleave untagged FLAGS updates; take the first (meta, message-bytes) tuple.
    part = next(p for p in data if isinstance(p, tuple) and len(p) == 2)
    meta = part[0].decode(errors="replace") if isinstance(part[0], bytes) else str(part[0])
    thrid = re.search(r"X-GM-THRID (\d+)", meta)
    return email.message_from_bytes(part[1], policy=policy.default), thrid.group(1) if thrid else ""


def plain_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    part = msg.get_body(preferencelist=("html",))
    if part is None:
        return ""
    html = part.get_content()
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", "", html))


def norm(text: str) -> list[str]:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def describe(msg: EmailMessage, thrid: str) -> dict:
    return {k: str(msg[k]) if msg[k] else None for k in
            ("From", "To", "Cc", "Reply-To", "Subject", "Date", "Message-ID", "In-Reply-To", "References")} | {
        "X-GM-THRID": thrid}


def audit(m, tag: str, expected: dict) -> dict | None:
    nums = search(m, f'"{tag}" from:{FO_SENDER}')
    print(f"[audit] messages from {FO_SENDER} with {tag}: {len(nums)}")
    if not nums:
        return None
    msg, thrid = fetch(m, nums[0])
    head = describe(msg, thrid)
    body = plain_text(msg)
    exp_lines, got_lines = norm(expected["email_body"]), norm(body)
    diff = list(difflib.unified_diff(exp_lines, got_lines, "asked_for", "fo_sent", lineterm=""))
    result = {
        "headers": head,
        "subject_exact": head["Subject"] == expected["subject"],
        "tag_kept": tag in (head["Subject"] or ""),
        "body_verbatim": exp_lines == got_lines,
        "body_contains_ours": "\n".join(exp_lines) in "\n".join(got_lines),
        "diff": diff,
        "to_ok": SELLER in (head["To"] or "") + (head["Cc"] or ""),
    }
    print(json.dumps(result, indent=2))
    return result


def wait_for_reply(m, tag: str, minutes: float, interval: int = 30) -> dict | None:
    deadline = time.time() + minutes * 60
    while True:
        m.noop()
        nums = search(m, f'"{tag}" from:{SELLER}')
        if nums:
            msg, thrid = fetch(m, nums[-1])
            raw = plain_text(msg)
            clean = EmailReplyParser.parse_reply(raw).strip()
            out = {"headers": describe(msg, thrid), "clean_text": clean, "raw_text_chars": len(raw)}
            print(json.dumps(out, indent=2))
            return out
        if time.time() > deadline:
            print(f"[reply] none after {minutes} min")
            return None
        print(f"[reply] {time.strftime('%H:%M:%S')} nothing yet; next check in {interval}s", flush=True)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag")
    ap.add_argument("--query", help="free X-GM-RAW search, prints matching messages (connectivity check)")
    ap.add_argument("--minutes", type=float, default=15)
    args = ap.parse_args()
    m = connect()
    print("[imap] connected, All Mail opened read-only")

    if args.query:
        for num in search(m, args.query)[:10]:
            msg, thrid = fetch(m, num)
            print(json.dumps(describe(msg, thrid), indent=2))
            print("CLEAN:", EmailReplyParser.parse_reply(plain_text(msg)).strip()[:500], "\n" + "-" * 60)
        return

    expected = next(e for e in json.loads((LOG_DIR / f"send_{args.tag}.json").read_text()) if e["kind"] == "built")
    report = {"tag": args.tag, "audit": audit(m, args.tag, expected)}
    report["reply"] = wait_for_reply(m, args.tag, args.minutes)
    if report["audit"] and report["reply"]:
        sent_id = report["audit"]["headers"]["Message-ID"]
        rh = report["reply"]["headers"]
        report["threading"] = {
            "tag_in_reply_subject": args.tag in (rh["Subject"] or ""),
            "in_reply_to_matches_cc_copy": sent_id is not None and sent_id == rh["In-Reply-To"],
            "references_include_cc_copy": sent_id is not None and sent_id in (rh["References"] or ""),
            "same_gmail_thread": report["audit"]["headers"]["X-GM-THRID"] == rh["X-GM-THRID"],
        }
        print(json.dumps(report["threading"], indent=2))
    (LOG_DIR / f"gmail_{args.tag}.json").write_text(json.dumps(report, indent=2, default=str))
    m.logout()


if __name__ == "__main__":
    main()
