# Gmail sent-copy audit and brief acceptance

Implemented locally on 2026-10-18. The new auditor was exercised read-only on
the existing `smoke_01` email. A new brief-mode live send still needs approval.

## Actual evidence

`sealed.ingest.audit.GmailAuditor` retrieves the actual sent copy from the
authenticated Gmail connection. It never sends mail or changes mailbox flags.
It evaluates full decoded body text, **not** the quote/signature-stripped reply
text. The first audit of the legacy live email confirmed sender authentication,
recipient, CC and subject; only `body_exact` failed because of the appended Fo
footer. Repeating it produced the same observation ID. The existing Fo state,
review marker and pending seller reply were unchanged.

An `AuditRequest` binds an exact `Outbound` payload to its attempted UTC time,
expected sender addresses, and optional known RFC Message-ID/Gmail thread.
The default candidate window is thirty minutes. Without a pinned RFC ID, the
auditor searches expected senders and associates candidates by subject tag or
known Gmail thread; it never guesses among multiple possible outbounds. Each
search and its union are bounded to 1,000 messages. Altered recipient/CC/subject
values are evaluated after retrieval, not filtered away by a perfect-match query.
If both tag and thread are lost with no known RFC ID, the result stays missing,
not passed. Late mail outside the bounded window needs explicit investigation.

Checks cover:

- A unique copy, exact decoded recipient/CC lists and subject, no visible BCC,
  expected Reply-To, send window and well-formed headers.
- Expected sender plus aligned Gmail DMARC evidence. Own-account outgoing mail
  may instead use Gmail's server-provided `\Sent` metadata; a message header
  cannot assert that label. Fo-origin copies do not need the owner's Sent label.
- The job-injected `Guard` checks the actual body and decoded routing headers,
  including display names. Brief-mode uses content/price-role constraints and
  permits safe wording and the ordinary known Fo footer. Legacy exact-send
  records retain their original normalized body-equality check.
- Every inline plain/HTML alternative is checked, not just the preferred text
  part. Decoded HTML source is also scanned for private data in attributes.
  Unknown links, attachments and unapproved inline binary content require review.
  Verified Fo logo/social-icon asset URLs are allowed alongside the known footer
  destinations; normalization permits their observed www/trailing-slash variants,
  not arbitrary paths, queries or user information.

Both metadata and body fetches use UID plus stable Gmail message identity; a
different UID or Gmail ID fails rather than attaching a body to wrong evidence.
Visible headers cannot reveal BCC recipients that the mail service omits. A sent
copy establishes evidence in the owner's mailbox, not proof that the dealer
read it. Content rules retain the documented natural-language coverage limits.

## Results, persistence and engine integration

`EmailAudit.state` is `pending`, `passed` or `review`. A copy absent within the
window is pending; after the window it requires review. Multiple candidates,
authentication failures or content/routing violations also require review.
Reports contain safe rule booleans, fingerprints and Gmail evidence metadata,
never body excerpts, raw errors or unsafe diff text.

`AuditStore` appends account-scoped observations to gitignored
`.sealed/audits.sqlite3`. Identical repeated observations are deduplicated;
pending-to-passed or new-ambiguity observations remain separate history.
`reply_binding()` automatically supplies real RFC/thread metadata only after
a passed audit of the same payload. It cannot promote failed legacy evidence.

Inject `FoTransport(..., sent_auditor=callback)` for live email. The callback
receives `(outbound, attempted_at)` while the account lock is held. Before
calling it, the adapter durably records `sent`, an active marker and
`audit_pending`. A passed audit permits normal completion only if there was
no other flag. Pending/failed audits, callback errors and wrong payload results
retain the pause without resending. A successful email audit never clears a
separate payment/unrequested-action flag. Replays return the recorded audit.

The engine accepts this result through `SendResult.audit` or
`Engine.on_email_audit()`, checks job/run/message/fingerprint and successful-send
association, and applies an observation once. Failed final audits block further
outreach, including declines after a booking. Events carry the real Gmail
evidence reference and safe checks, not the untrusted body.

The adapter's original body-only `sent_copy` hook remains for simulation.
The [background coordinator](coordinator.md) now wires the audited dispatch path; the [job API and dashboard](dashboard.md) now use this worker;
production callers must supply the auditor rather than treating a Fo status or
preview as delivered-copy evidence.

## Controlled brief-flow command

From `backend/`, review this dry run first:

```powershell
.\.venv\Scripts\python.exe -m sealed.fo.brief_smoke --to consenting-teammate@example.org
```

CC defaults to `GMAIL_ADDRESS`; for live auditing it must match that account.
The printed brief requests a sample written out-the-door total for a controlled
test, all fees/taxes, availability and quote validity. There is no real buying
intent, target or ceiling. Fo chooses the email wording, openly as an AI
assistant. The default `brief_01` identity belongs to `fo-brief-smoke`, separate
from the immutable legacy `smoke_01` test.

Only after explicit approval, append `--yes-send`. Gmail read-only access is
established before any Fo send; the adapter checks the draft, sends once and
waits briefly for a passed Gmail audit. A passed result writes trusted reply
metadata to `.sealed/brief_binding_brief_01.json`. State and audit paths may be
overridden for tests; use the same real account state file in live operation.

An existing account pause stops before connecting to Fo. This command does not
offer a force/unpause flag, delete historical records or create an automatic
retry run. The existing legacy signature warning requires explicit reviewed
reconciliation before a new test. Keep its original audit result as history.

To recheck a recorded brief attempt without any Fo call or another send:

```powershell
.\.venv\Scripts\python.exe -m sealed.fo.brief_smoke --to consenting-teammate@example.org --audit-only
```

Read-only rechecking appends evidence and preserves the account review state.
It does not automatically resume a paused job. Public restart-safe job state and
interactive private-vault restoration are now implemented in the [coordinator](coordinator.md);
review/resume controls still require reviewed reconciliation tooling.
