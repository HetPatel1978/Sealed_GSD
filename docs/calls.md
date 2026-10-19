# Checked call plans and call-result tracking

New calls use a checked plan before permission to dial. Existing legacy attempts
keep their original identities and fingerprints; this change never reissues an
uncertain call or resumes a cancelled/paused negotiation.

## Before dialing

Sealed asks Fo to return a strict inline JSON plan containing the job's exact
buyer name, opening, phone, brief and a job/run/message request identifier. The
plan must state that no call has started, no follow-ups were scheduled and the
limit is one call. Code compares every field against the intended plan and runs
the guard before issuing one separate dialing instruction. A changed buyer,
phone, opening or brief cannot receive permission to dial. A malformed response
or evidence that Fo acted during planning requires review.

The required opening is `I'm Fo, an AI assistant calling on behalf of BUYER.`
Instructions explicitly forbid substituting an account owner or a remembered
buyer, retries, reminders, tasks, callbacks, other outreach, payment and booking.
The requested facts still exclude the private ceiling. Call prices are supplied
only when authorized by the existing strategy and verified written ledger.

## While the call runs

An acknowledgement such as "On it, calling ..." records a durable `call_pending`
outbox status and one `call.pending` event. It does not mark the call completed,
create a quote or immediately pause the job. The API exposes worker status
`call_in_progress` and dealer state `CALL_IN_PROGRESS`; the dashboard shows
**Waiting for Fo's call result**. Another job can report `fo_busy_call` while this
account's call is being observed. Later outreach/timers wait for the pending call.

Each observation reads the saved Fo conversation. It never asks again, repeats
dialing permission, requests a retry or sends a follow-up. A crash after dial
permission but before the outbox receipt is recoverable through the same read-only
path. After restart, existing-call observation works without IMAP, new outreach
permission or a restored private ceiling. New job actions still require their
normal authorizations and private state.

Cancelling a Sealed job stops queued actions while preserving observation of a
call that may already be running. Reading its result does not resume the job.
Sealed cancellation does not recall a call or automatically cancel any task that
Fo independently created. See [operator-reviewed recovery](call_review.md).

## When the result arrives

The final inline report must match the request identifier, phone and buyer, state
the actual outcome and introduction, declare whether follow-ups were scheduled,
and include a complete voice transcript. Only assistant reports for the exact
request are considered; another call's result or user-supplied JSON is ignored.
Duplicate JSON keys and extra schema fields are rejected.

Code checks the reported introduction and actual assistant transcript against
the buyer, plus unsupported numbers, commitments, payment topics and promised
follow-ups. Missing evidence, identity deviations, a different call scope or a
reported retry raises review and retains the account reservation. No automatic
correction call or repeated permission is issued. Read failures retain the claim;
after 30 minutes without a usable final report it requires review as
`call_result_missing`. A new natural-language scheduling report also raises review.

A safe completed attempt emits `call.completed` and the existing `message.sent`
event for the call brief. The event source is explicitly `fo_report`. Outcomes
are `connected`, `voicemail`, `no_answer` or `failed`; a no-answer/voicemail attempt
does not claim that the dealer was contacted. No spoken price becomes a firm
quote, enters the written-price ledger or creates an offer. Confirmation still
requires the dealer's authenticated written terms.

Public call events contain static outcome/identity/flag metadata. Raw voice
transcripts stay in gitignored Fo state instead of being copied into events or
the outbox receipt. Identity is marked `not_spoken` when a complete no-answer
report contains no introduction; it is not described as a verified introduction.

## Validation and provider limits

The controlled tests cover acknowledgments, one dialing permission, restart and
dispatch-claim recovery, observation after cancellation or missing private state,
mail outages, incorrect buyer/phone/intro, stale reports, missing transcripts,
unrequested follow-ups and bounded read failures. A full offline acceptance runs
email RFQ -> call nudge -> verified written quote -> counter -> explicit user
approval -> written confirmation. Backend: 586 tests; frontend: 9 tests, build
and lint pass.

These are preflight checks and checks on Fo-reported evidence. The existing MCP
connection does not expose a mechanically restricted call tool or independently
attested voice/task state. Sealed cannot guarantee that Fo will obey the plan or
report every action. A fresh live test of this new protocol with a consenting
stand-in is still required; implementing it did not place new dealer calls.
