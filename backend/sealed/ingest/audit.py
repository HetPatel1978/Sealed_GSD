"""Read-only Gmail sent-copy auditing, with safe durable reports.

The caller injects its job's Guard; no ceiling or private-store access exists here.
MailMessage objects must come from authenticated Gmail retrieval, not uploads.
"""

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sealed.core.guard import Guard
from sealed.core.email_policy import fo_signature_link
from sealed.core.models import EmailAudit, EmailDraft, MailEvidence, Outbound, outbound_fingerprint
from sealed.ingest.gmail import GmailReader
from sealed.ingest.mail import Binding, MailMessage, addresses, message_ids

ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}")


@dataclass(frozen=True)
class AuditRequest:
    outbound: Outbound
    attempted_at: str
    expected_senders: tuple[str, ...]
    sent_message_id: str = ""
    thread_id: str = ""
    window_seconds: int = 1800

    def __post_init__(self):
        if (self.outbound.channel != "email" or not self.outbound.job_id or not self.outbound.run_id
                or len(addresses([self.outbound.to])) != 1
                or not self.expected_senders or any(not ADDRESS.fullmatch(a) for a in self.expected_senders)
                or datetime.fromisoformat(self.attempted_at).tzinfo is None
                or not 1 <= self.window_seconds <= 86400
                or len(re.findall(r"\[SEALED-[A-Za-z0-9_-]+\]", self.outbound.subject or "")) != 1
                or (self.sent_message_id and message_ids(self.sent_message_id) != (self.sent_message_id,))
                or (self.sent_message_id and not re.fullmatch(r"<[A-Za-z0-9._+@=/-]+>", self.sent_message_id))
                or (self.thread_id and not self.thread_id.isdigit())):
            raise ValueError("Invalid sent-copy audit request.")

    @property
    def tag(self) -> str:
        return re.findall(r"\[SEALED-[A-Za-z0-9_-]+\]", self.outbound.subject or "")[0]

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.attempted_at)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.window_seconds)


def audit_copies(request: AuditRequest, messages: list[MailMessage], mailbox: str,
                 guard: Guard, *, now: datetime | None = None) -> EmailAudit:
    """Evaluate complete candidates without ever returning raw content or diff excerpts."""
    msg = request.outbound
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Audit time requires a timezone.")
    senders = {a.lower() for a in request.expected_senders}
    if request.sent_message_id:
        candidates = [m for m in messages if m.message_id == request.sent_message_id]
    else:
        candidates = [m for m in messages if m.from_addr in senders
                      and request.start <= datetime.fromisoformat(m.received_at) <= request.end
                      and (request.tag in m.subject or (request.thread_id and m.thread_id == request.thread_id))]
    candidates = list({m.gmail_id: m for m in candidates}.values())
    refs = tuple(sorted(m.evidence_ref for m in candidates))
    basis = "brief_constraints" if msg.email_brief is not None else "exact_payload"

    def result(state, reason, *, rules=(), evidence=None, passed=False):
        return EmailAudit(msg.job_id, msg.run_id, msg.message_id, outbound_fingerprint(msg),
                          state, reason, basis, bool(candidates), passed, tuple(rules), refs, evidence)

    if not candidates:
        return result("pending" if now <= request.end else "review",
                      "audit_pending" if now <= request.end else "audit_missing")
    if len(candidates) != 1:
        return result("review", "audit_ambiguous", rules=({"rule": "unique_sent_copy", "passed": False},))
    copy = candidates[0]
    owner_sent = copy.from_addr == mailbox.lower() and "\\Sent" in copy.gmail_labels
    authenticated = copy.authenticated or owner_sent
    auth_source = "gmail_dmarc" if copy.authenticated else "gmail_sent_label" if owner_sent else "unverified"
    expected_to = tuple(sorted(addresses([msg.to])))
    expected_cc = tuple(sorted(addresses([msg.cc])))
    to_ok = tuple(sorted(copy.to)) == expected_to and bool(expected_to)
    cc_ok = tuple(sorted(copy.cc)) == expected_cc and mailbox.lower() in copy.cc
    # Preserve configured address spelling for the draft policy, after comparing
    # actual decoded mailbox lists. Display names are scanned via routing_text.
    actual = EmailDraft(msg.to if to_ok else ", ".join(copy.to), msg.cc if cc_ok else ", ".join(copy.cc),
                        copy.subject, copy.raw_text)
    content = guard.check_delivered(msg, actual, extra_headers=copy.routing_text + "\n" + copy.mime_text)
    for text in set(copy.audit_texts) - {copy.raw_text}:
        alternative = guard.check_delivered(msg, replace(actual, body=text))
        alternatives = {rule["rule"]: rule["passed"] for rule in alternative.rules}
        for rule in content.rules:
            rule["passed"] &= alternatives.get(rule["rule"], True)
    content_passed = all(rule["passed"] for rule in content.rules)
    allowed_mailto = {"mailto:" + address for address in senders | {mailbox.lower(), *expected_to, *expected_cc}}
    links_ok = all(fo_signature_link(link, images=True) or link in allowed_mailto or link.startswith(("cid:", "#"))
                   for link in copy.html_links)
    flags = set(copy.flags) - ({"sender_auth_unverified"} if owner_sent else set())
    checks = [
        {"rule": "unique_sent_copy", "passed": True},
        {"rule": "sender_expected", "passed": copy.from_addr in senders},
        {"rule": "sender_authenticated", "passed": authenticated},
        {"rule": "recipient_exact", "passed": to_ok},
        {"rule": "cc_exact", "passed": cc_ok},
        {"rule": "bcc_absent", "passed": not copy.bcc},
        {"rule": "reply_to_expected", "passed": all(a in senders | {mailbox.lower()} for a in copy.reply_to)},
        {"rule": "subject_exact", "passed": copy.subject == msg.subject},
        {"rule": "send_window", "passed": request.start <= datetime.fromisoformat(copy.received_at) <= request.end},
        {"rule": "message_well_formed", "passed": not flags},
        {"rule": "no_unrequested_attachments", "passed": not copy.has_attachments},
        {"rule": "html_links_in_scope", "passed": links_ok},
        *([{"rule": "reply_linked", "passed": msg.reply_to_message_id in copy.reply_to_ids}]
          if msg.reply_to_message_id is not None else []),
        *([{"rule": "reply_thread_expected", "passed": msg.reply_thread_id == copy.thread_id}]
          if msg.reply_thread_id is not None else []),
        *content.rules,
    ]
    if msg.email_brief is None:
        checks.append({"rule": "body_exact", "passed": all(" ".join(text.split()) == " ".join(msg.body.split())
                                                            for text in (copy.raw_text, *copy.audit_texts))})
    passed = all(rule["passed"] for rule in checks)
    evidence = MailEvidence(copy.gmail_id, copy.thread_id, copy.message_id, copy.received_at,
                            copy.spf, copy.dkim, copy.dmarc, auth_source)
    return result("passed" if passed else "review", None if passed else "audit_mismatch",
                  rules=checks, evidence=evidence, passed=content_passed)


def reply_binding(request: AuditRequest, result: EmailAudit) -> Binding:
    """Only a passed audit may automatically establish real reply-link metadata."""
    if not result.validates(request.outbound) or not result.matched or result.evidence is None:
        raise ValueError("A passed audit of this exact outbound is required for reply binding.")
    msg, evidence = request.outbound, result.evidence
    return Binding(msg.job_id, msg.run_id, msg.dealer_id, addresses([msg.to])[0], request.tag,
                   request.attempted_at, msg.message_id, evidence.message_id, evidence.thread_id)


class AuditStore:
    """Append-only, account-scoped observations. Re-polling an unchanged result is a no-op."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS email_audits (
            account TEXT NOT NULL, observation_id TEXT NOT NULL, record TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account, observation_id))""")
        self.db.commit()

    def append(self, account: str, result: EmailAudit) -> bool:
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO email_audits (account, observation_id, record) VALUES (?,?,?)",
                                     (account.lower(), result.observation_id, json.dumps(asdict(result))))
        return cursor.rowcount == 1

    def records(self, account: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT record FROM email_audits WHERE account=? ORDER BY rowid", (account.lower(),))]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()


class GmailAuditor:
    def __init__(self, reader: GmailReader, guard: Guard, store: AuditStore, *, clock=None):
        self.reader, self.guard, self.store = reader, guard, store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def audit(self, request: AuditRequest) -> EmailAudit:
        # With no RFC anchor, search each expected sender in the bounded window.
        # Do not require correct recipients/subject in the query: an altered email
        # must be retrieved and fail auditing rather than disappear from evidence.
        if request.sent_message_id:
            queries = [f"rfc822msgid:{request.sent_message_id[1:-1]}"]
        else:
            queries = [f"from:{sender} after:{int((request.start - timedelta(days=1)).timestamp())} "
                       f"before:{int((request.end + timedelta(days=1)).timestamp())}"
                       for sender in sorted(set(request.expected_senders))]
        uids = set()
        for query in queries:
            uids.update(self.reader.search(query))
        # Reader already bounds each search; also bound the union.
        if len(uids) > 1000:
            from sealed.ingest.gmail import GmailError
            raise GmailError("Too many sent-copy candidates; narrow the audit window.")
        copies = [self.reader.fetch(uid) for uid in sorted(uids, key=int)]
        result = audit_copies(request, copies, self.reader.address, self.guard, now=self.clock())
        self.store.append(self.reader.address, result)
        return result

    def wait(self, request: AuditRequest, *, seconds: float = 30,
             monotonic: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep) -> EmailAudit:
        if not 0 <= seconds <= 60:
            raise ValueError("Audit wait must be between zero and sixty seconds.")
        deadline = monotonic() + seconds
        while True:
            result = self.audit(request)
            remaining = deadline - monotonic()
            if result.state != "pending" or remaining <= 0:
                return result
            sleep(min(3, remaining))
