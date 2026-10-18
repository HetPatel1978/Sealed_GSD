"""One read-only Gmail poll; stage replies locally without invoking the engine.

    python -m sealed.ingest.gmail --bindings ../.sealed/mail_bindings.json

Bindings are a JSON list of trusted Binding records (see docs/gmail_ingestion.md).
Never imports the spike, sends mail, changes flags or automatically learns senders.
"""

import argparse
import imaplib
import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from sealed.env import REPO_ROOT, load_env
from sealed.ingest.mail import Binding, MailMessage, decode_message, match_message
from sealed.ingest.store import InboxStore

MAX_MESSAGE_BYTES = 1_000_000
MAX_MATCHES = 1000


class GmailError(RuntimeError):
    """Sanitized failure safe for a local CLI/status event."""


class GmailReader:
    def __init__(self, address: str, password: str, *, factory=None):
        if not address or not password:
            raise GmailError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD before reading Gmail.")
        self.address = address.strip().lower()
        self.connection = None
        try:
            factory = factory or imaplib.IMAP4_SSL
            self.connection = factory("imap.gmail.com", timeout=25)
            self._ok(self.connection.login(address, password.replace(" ", "")))
            self._ok(self.connection.select('"[Gmail]/All Mail"', readonly=True))
        except (imaplib.IMAP4.error, OSError, GmailError):
            self.close()
            raise GmailError("Gmail connection failed; check credentials and IMAP access.") from None

    @staticmethod
    def _ok(result):
        status, data = result
        if status != "OK":
            raise GmailError("Gmail rejected the read-only request.")
        return data

    def search(self, query: str) -> list[str]:
        if any(c in query for c in "\r\n\x00"):
            raise ValueError("Invalid Gmail query.")
        quoted = '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'
        try:
            data = self._ok(self.connection.uid("SEARCH", None, "X-GM-RAW", quoted))
            uids = data[0].decode("ascii").split() if data and data[0] else []
        except (imaplib.IMAP4.error, OSError, UnicodeError):
            raise GmailError("Gmail search failed; pending replies were preserved.") from None
        if not all(uid.isdigit() for uid in uids):
            raise GmailError("Invalid Gmail UID response.")
        if len(uids) > MAX_MATCHES:
            raise GmailError("Too many matching emails; narrow the outreach time window before retrying.")
        return uids

    def fetch(self, uid: str) -> MailMessage:
        if not uid.isdigit():
            raise ValueError("Invalid Gmail UID.")
        try:
            meta = self._ok(self.connection.uid("FETCH", uid, "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS RFC822.SIZE INTERNALDATE)"))
            metadata = b" ".join(part for part in meta if isinstance(part, bytes))
            size = re.search(rb"RFC822.SIZE (\d+)", metadata)
            gm_id = re.search(rb"X-GM-MSGID (\d+)", metadata)
            thread = re.search(rb"X-GM-THRID (\d+)", metadata)
            metadata_uid = re.search(rb"\bUID (\d+)\b", metadata)
            received = re.search(rb'INTERNALDATE "([^"\r\n]+)"', metadata)
            if not all((size, gm_id, thread, received, metadata_uid)):
                raise GmailError("Gmail message metadata is incomplete; no reply was consumed.")
            if metadata_uid.group(1).decode() != uid:
                raise GmailError("Gmail returned a different message; retry the poll.")
            if int(size.group(1)) > MAX_MESSAGE_BYTES:
                raise GmailError("Matching email exceeds the ingestion size limit; review it manually.")
            parts = self._ok(self.connection.uid("FETCH", uid, "(UID X-GM-MSGID BODY.PEEK[])"))
            literals = [part for part in parts if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes)]
            if len(literals) != 1 or not literals[0][1] or len(literals[0][1]) > MAX_MESSAGE_BYTES:
                raise GmailError("Gmail returned an invalid or oversized message body.")
            if len(literals[0][1]) != int(size.group(1)):
                raise GmailError("Gmail returned an incomplete message body; retry the poll.")
            # UID fetch plus an explicit UID check prevents sequence-number races.
            returned_uid = re.search(rb"\bUID (\d+)\b", literals[0][0])
            if not returned_uid or returned_uid.group(1).decode() != uid:
                raise GmailError("Gmail returned a different message; retry the poll.")
            returned_id = re.search(rb"\bX-GM-MSGID (\d+)\b", literals[0][0])
            if not returned_id or returned_id.group(1) != gm_id.group(1):
                raise GmailError("Gmail returned inconsistent message identity; retry the poll.")
            labels = re.search(rb"X-GM-LABELS \(([^)]*)\)", metadata)
            # Only the server's system Sent label matters for own-account mail
            # which may not have an inbound Authentication-Results header.
            sent = bool(labels and re.search(rb'(?:^|[\s"])\\Sent(?:$|[\s"])', labels.group(1)))
            received_at = parsedate_to_datetime(received.group(1).decode()).astimezone(timezone.utc).isoformat()
            return decode_message(literals[0][1], gmail_id=gm_id.group(1).decode(), uid=uid,
                                  thread_id=thread.group(1).decode(), received_at=received_at,
                                  gmail_labels=("\\Sent",) if sent else ())
        except (imaplib.IMAP4.error, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise GmailError("Gmail message could not be decoded; no reply was consumed.") from None

    def close(self):
        if self.connection is not None:
            try:
                self.connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@dataclass(frozen=True)
class PollResult:
    fetched: int
    staged: int
    duplicates: int
    ignored: int
    review: int


def poll(reader: GmailReader, store: InboxStore, bindings: list[Binding]) -> PollResult:
    uids = set()
    # Sender searches also find replies that lost the tag and thread. Matching
    # below still requires known identity/time and flags fallback for review.
    for sender in sorted({b.sender.lower() for b in bindings}):
        since = min(datetime.fromisoformat(b.sent_at) for b in bindings if b.sender.lower() == sender)
        query = f"from:{sender} after:{int((since - timedelta(days=1)).timestamp())}"
        uids.update(reader.search(query))
    if len(uids) > MAX_MATCHES:
        raise GmailError("Too many matching emails; narrow the outreach time window before retrying.")
    staged = duplicates = ignored = review = 0
    for uid in sorted(uids, key=int):
        message = reader.fetch(uid)
        match = match_message(message, bindings, reader.address)
        if match.status == "ignored":
            ignored += 1
        elif store.stage(reader.address, message, match):
            staged += 1
            review += match.status == "review"
        else:
            duplicates += 1
    return PollResult(len(uids), staged, duplicates, ignored, review)


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=REPO_ROOT / ".sealed" / "inbox.sqlite3")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.bindings.read_text(encoding="utf-8"))
        if not isinstance(config, list) or not config:
            raise ValueError("Bindings must be a nonempty JSON list.")
        bindings = [Binding(**item) for item in config]
    except (OSError, ValueError, TypeError):
        parser.error("Could not load valid outreach bindings; see docs/gmail_ingestion.md.")
    try:
        with GmailReader(os.getenv("GMAIL_ADDRESS", ""), os.getenv("GMAIL_APP_PASSWORD", "")) as reader:
            with InboxStore(args.state) as store:
                result = poll(reader, store, bindings)
                print(json.dumps({**result.__dict__, "pending": len(store.pending(reader.address)),
                                  "pending_review": len(store.pending(reader.address, status="review")),
                                  "mailbox_read_only": True}))
    except (GmailError, socket.timeout) as error:
        print(str(error) if isinstance(error, GmailError) else "Gmail timed out; retry the poll.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
