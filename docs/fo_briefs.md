# Fo composes email from a bounded brief

Approved on 2026-10-18 (decision 16). New job emails default to this contract.
Sealed supplies the objective, routing, public job facts and permitted prices;
Fo chooses the wording, greeting and an ordinary signature. Calls continue to
use their existing briefs.

## Division of responsibility

| Sealed | Fo |
|---|---|
| Authorizes each outreach action and its channel | Composes natural wording for that action |
| Supplies verified own/competing quotes and the permitted offer | Asks the seller for the information specified in the brief |
| Keeps the ceiling private; offers remain the public target | Uses only supplied facts and price roles |
| Validates written replies and qualifies proposals | Sends one reviewed draft after confirmation |
| Requires approval of exact terms before requesting paperwork | Never independently pays, reserves, books visits or schedules follow-ups |

Fo is not given blanket authority to run an unattended negotiation. The fixed
round plan and per-action authorization remain in Sealed, so changes in the
private ceiling cannot affect offers or message timing. Raw seller instructions
are not forwarded to Fo as trusted facts.

## Brief and draft contract

`messages.build()` now returns an `Outbound` with a typed `EmailBrief`.
`body` contains the brief, **not an email for the seller**. The brief includes
the goal, buyer name, exact specification, ZIP, timeline, currency and only the
prices authorized for this action, with separate own-quote, competing-quote,
offer and approved-total roles. The public target appears only when offered.

The instruction explicitly asks Fo to draft without sending. Fo returns:

```text
<<<EMAIL_DRAFT
To: seller@example.org
CC: buyer@example.com
Subject: the exact supplied subject including its tag
Body:
Fo's composed email
EMAIL_DRAFT>>>
Good to send?
```

Live Fo also returns the closing marker as a whole line `EMAIL_DRAFT`, omitting
the trailing `>>>`. That observed variant is accepted with the same required
headers, anchored envelope and content checks. Partial markers, nested markers
and additional instructions outside the envelope remain rejected. The brief
also tells Fo to ignore earlier conversation facts that are absent from the
current brief; a reused conversation does not expand its authorized facts.

Code checks the whole draft and its actual routing before confirming it. The
reviewed draft is saved in the adapter's pending record before the confirmation
turn. Replays return the saved draft and suppress another send. Polling, account
serialization and durable reservations continue to apply. A send reported
without a reviewed draft remains recorded as sent, but pauses the account with
`audit_mismatch`; it is never retried automatically. Reported extra activity such
as a scheduled follow-up also raises review.

Draft rules cover exact recipient/CC/subject, AI disclosure, specification and
reply-all, action intent, permitted numbers/currency, explicit price roles,
commitment/payment wording, known unsupported claims and additional actions or
contacts. Prices must use digits and supplied currency, and each price role must
be stated in an unambiguous sentence. Public digit prices are masked before the
secret check in both dollar-prefixed and plain-digit formats, independently of
their distance from the private ceiling.

These checks are conservative rules, **not a semantic proof of arbitrary prose**.
They catch tested patterns, and may reject legitimate unfamiliar wording. They
cannot establish every possible natural-language claim as true. Missing draft
envelopes, ambiguous price attribution, unsupported numeric representations and
known unsafe language require review rather than a guessed approval. A controlled
live counter-reply has now passed draft checks and actual Gmail auditing. Natural
own-quote attribution and conditional requests for a written total have regression
coverage; affirmative buyer confirmations remain blocked.

## Sent copies and events

Brief-mode content checks allow compliant rewording, whitespace and ordinary
signatures. Fo's observed Wajo signature links are permitted; arbitrary new
contacts/links remain outside the brief. A signature alone is not an audit
failure. Post-send checks still look for unauthorized amounts, changed price
roles, wrong routing and commitments; they are not a word-for-word body diff.

`Guard.check_email()` accepts an `EmailDraft` representing either a preview or
an actual copy and checks the same constraints. The new [Gmail auditor](sent_audit.md)
retrieves full copies, validates actual routing/authentication and injects safe
evidence into the adapter/engine. The controlled `brief_smoke` command wires it
into the new send path; the background coordinator now connects it to durable dispatch; the [job API and dashboard](dashboard.md) now use that worker. A compliant preview is not evidence that the delivered email matched it.

For new emails, `message.sent` includes `brief` and `content_source`. `body` is
the reviewed Fo draft with `content_source: "fo_preview"`, or the empty string
with `"pending_gmail"` when the body is unknown. A brief is never presented as
the actual outgoing email. `numbers` describes the displayed body only.
`audit.checked.basis: "brief_constraints"` means `matched` is compliance with
the authorized brief, rather than equality to a template. No untrusted draft
excerpt or suspected secret is copied into an event's `fo_said` field.

## Legacy sends and validation

`email_mode="exact"` preserves exact-template regression checks and payload
fingerprints. The existing `sealed.fo.smoke` command remains a **legacy exact
test** so its recorded `smoke_01` attempt is not reinterpreted or resent.
Changing the contract does not release the existing account review marker or
rewrite historical audit evidence.

Run offline from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m sealed.sim.run
```

The simulator acts as a scripted Fo composer, validates separately composed
drafts and copies, and completes the normal approval/booking story. It does not
contact Gmail or Fo. Sent-copy auditing has now been exercised read-only against
the legacy email, and the brief-mode $90 counter to the reviewed $100 sample passed
live acceptance on October 18, 2026. General quote parsing, idempotent reply
delivery and the [background coordinator](coordinator.md) are now implemented
separately; this older demo uses reviewed sample extraction.
