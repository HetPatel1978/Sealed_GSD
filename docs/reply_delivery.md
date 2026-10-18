# Durable reply delivery

`sealed.ingest.deliver.ReplyDelivery` connects authenticated staged Gmail replies
and cached parser results to the existing negotiation engine. It constructs no
model or Fo client. Runtime callers provide trusted bindings, a checker, and a
qualifier bound inside the private zone.

## Transaction boundary

1. Register a public engine run in `DeliveryStore`. An existing run cannot be
   overwritten by registration. Its checkpoint excludes the transport, checker,
   qualifier and private vault; they must be rebound after restart.
2. Deliver a staged Gmail ID for the exact account/job/run. Identity and association
   are rechecked against trusted bindings, the configured dealer and the run tag.
   A parser cannot supply identity, phone-only attribution or source evidence.
3. Load the parse cache using the actual reply and public job context, including
   evaluation date and whether a linked booking was previously sent. Missing
   parsing pauses the run and clears approval without consuming the reply. No
   automatic model request is made by delivery.
4. Process the reply against an engine with `QueueTransport`. Real Gmail evidence
   and the real subject enter events. A guarded follow-up creates `message.queued`,
   never `message.sent` and never a Fo call inside this transaction.
5. Commit the reply receipt, engine checkpoint, appended events and outbox intents
   together in SQLite, then acknowledge the separately staged inbox item.

A crash before commit rolls back every effect and leaves the inbox pending. A crash
after commit but before acknowledgment reuses the saved receipt and acknowledges
without another quote, event or outreach intent. Receipts are unique per account
and Gmail ID and reject payload/scope conflicts. SQLite serializes workers using
separate connections. Use one connection per worker.

Quote ordering uses Gmail receipt time and Gmail ID as a tie-breaker. A late older
parsed reply remains historical evidence but cannot replace newer terms, change a
pending proposal, select a new reply thread or trigger a clarification.

## Outbox dispatch

`ReplyDelivery.dispatch` requires explicit runtime authorization and a supplied
transport. Delivery alone never authorizes sending. Before a pending intent is
dispatched, code rechecks the guard, current quote authority, quoted expiry and
whether a clarification was superseded by a firm reply. Obsolete intents are
suppressed without sending; outdated bookings and guard failures pause for review.

The outbox claim is committed as `dispatching` before the external transport runs.
No SQLite write transaction is held while Fo works. Reply application for that run
waits while a send is in flight. The account cannot start another outbox dispatch
until that claim settles. Afterward the actual result, engine changes, events and
any new intents commit together. Live email success requires a matching Gmail
audit; passed audits also create durable reply bindings with real RFC/thread IDs.

A crash or failed commit after an external send retains `dispatching`; another
dispatch is refused. Reconcile using Fo's durable account state and Gmail before
manual recovery. Never reset the claim, delete state or change IDs to force a retry.
Transport errors are sanitized. Raw provider notes, SDK exceptions and sent-copy
text are not saved in outbox outcome receipts. Checked draft content and safe audit
evidence follow the existing engine contract.

Queued booking does not start declines. Only the checked actual booking result
allows the engine to queue declines for other sellers. Failed/uncertain bookings
keep those sellers available. The original synchronous simulation path still runs.

## Controlled acceptance using the cached real reply

From `backend/`:

```powershell
python -m sealed.ingest.delivery_smoke
```

This command supports only the existing `fo-adapter-smoke/smoke_01` controlled test.
It imports public scope into a separate engine, copies the latest staged source
reply and reuses its approved parse cache. The copied inbox and engine/outbox live
in gitignored `.sealed/handoff_smoke/`. The original inbox is unchanged. No network
request, model request, Fo client, dispatch, timer or private ceiling is involved.

Acceptance on October 18, 2026 applied the real $95 reply once and queued one
`firm_up` clarification for its missing specification confirmation. The queued
reply is linked to the real incoming RFC Message-ID and Gmail thread. Immediate
replay produced no second quote or message; no send was claimed. The first report
is `.sealed/handoff_smoke/acceptance.json`; restart replays use `acceptance_replay.json`.

This verifies the handoff and durable outbox components. It does not start a
background negotiator or import the complete earlier email history as engine
events. The [background coordinator](coordinator.md) now wires polling, authorized
extraction, delivery, durable timers and authorized dispatch, with interactive
private-vault restoration after restart. The [job API and dashboard](dashboard.md) now provide HTTP/SSE and approval
controls. Reviewed reconciliation remains. The fixed two-round strategy is unchanged.
