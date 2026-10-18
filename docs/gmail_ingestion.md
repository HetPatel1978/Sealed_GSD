# Gmail reply ingestion

The first part of build-plan step 3 is implemented in `backend/sealed/ingest/`.
It stages seller replies locally. It does not yet parse quotes, invoke the engine,
send messages, or resolve the existing Fo sent-copy audit warning.

## Run one poll

Set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in the repo-root `.env`. Create a
private `.sealed/mail_bindings.json` with a list of **known sent emails**, for example:

```json
[
  {
    "job_id": "demo",
    "run_id": "run_01",
    "dealer_id": "seller_01",
    "sender": "seller@example.org",
    "tag": "[SEALED-DEMO1234]",
    "sent_at": "2026-10-18T12:00:00+00:00",
    "outbound_id": "rfq_01",
    "outbound_message_id": "<actual-sent-copy@example.org>",
    "thread_id": "123456789"
  }
]
```

`sent_at` is the actual outreach time, with a timezone. `outbound_id` is Sealed's
internal message ID; `outbound_message_id` is the real RFC Message-ID from the
Gmail sent copy. A later sent-copy audit will supply this metadata automatically.
Until then, only use reviewed sent-copy evidence to create these bindings. Both
RFC Message-ID and Gmail thread ID are optional; a known sender plus unique tag
can still match a reply. There must be no ceiling or credentials in this file.

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m sealed.ingest.gmail --bindings ../.sealed/mail_bindings.json
```

The default local database is `.sealed/inbox.sqlite3` (gitignored). CLI output
contains counts only. `staged` counts newly saved messages, including `review`;
`pending` counts parser-ready replies and `pending_review` counts quarantined
messages. Repeat the command to check deduplication: the same Gmail message has
`staged: 0` and increments `duplicates`, while pending work remains available.

## Matching and trust

- Gmail is opened read-only. All searches/fetches use UIDs; body reads use
  `BODY.PEEK[]`, and no mailbox flags are modified. A timeout or fetch/decode
  failure stops the poll without consuming any staged replies.
- Search is limited to configured sender addresses after the outreach window.
  Matching uses the trusted Gmail received time, not the sender's `Date` header.
  At most 1,000 matches and 1 MB per message are accepted. Exceeding a limit
  requires narrowing the window or reviewing the oversized message; nothing
  is silently truncated.
- Match order is subject tag, RFC reply references, then known Gmail thread.
  Sender plus time alone is review-only. Conflicting tags/threads, multiple
  possible jobs, and messages not addressed to the configured mailbox require
  review. A tag alone cannot establish a phone-only dealer's identity.
- Automatic readiness requires a matching known sender and Gmail's first
  `Authentication-Results` header to report DMARC pass aligned with that sender's
  exact domain. SPF/DKIM results are retained. Authentication headers are trusted
  only at the authenticated Gmail retrieval boundary; callers must not pass
  arbitrary uploaded `.eml` files as Gmail evidence. Later forged headers and
  ARC headers never override that first result. Forwarded mail without this
  evidence remains review-only.
- Plain text is preferred; HTML is converted locally without loading assets.
  Attachments, scripts, HTML blockquotes, Gmail quote blocks, and common plain
  text/Outlook history are excluded from `clean_text`. `raw_text` retains the
  decoded conversation text for review. Unrecognized inline quote formats may
  still require human review; this stage makes no claim that a total is firm.
- Internal outbound linkage is recorded only when real RFC reply headers name
  a known sent message. A shared Gmail thread alone cannot confirm a booking.

## Durable handoff

`InboxStore.pending(account)` returns ready records containing cleaned and raw
text, real headers, authentication results, matching metadata, and
`evidence_ref: "gmail:<X-GM-MSGID>"`. Review records are fetched separately with
`status="review"`. The database primary key is the normalized account and stable
Gmail message ID, so a UID change or process restart cannot duplicate a reply.
Independent connections also share that uniqueness constraint.

Fetching/staging never acknowledges a reply. A future parser/engine consumer
must first persist its result idempotently under the evidence reference, then
call `acknowledge(account, gmail_id)`. This is an at-least-once handoff; the
downstream consumer must deduplicate a replay after a crash between those steps.
Acknowledgement retains the dedupe record. Review records cannot be acknowledged
through the ready-record API and never become ready just because of a repoll.

## Remaining in step 3

1. Replace the manual live sent-copy check with reusable auditing and real sent
   metadata; preserve the current Fo signature mismatch for review.
2. Add the isolated quote parser and deterministic validation.
3. Consume staged replies idempotently into the engine with real evidence,
   subject, authentication and timestamps; exercise the sample reply end to end.
