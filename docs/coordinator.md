# Background negotiation coordinator

New checked-plan calls persist an in-progress outbox receipt and are observed
through their existing Fo conversation, including after restart/cancellation.
Observation is read-only and can run without IMAP or private-ceiling restoration.
Pending calls hold later account outreach and timers; spoken prices never become
written offers. See [the call lifecycle and limits](calls.md).

`sealed.service` connects Gmail staging, isolated quote parsing, durable engine
delivery, scheduled round transitions and guarded Fo dispatch. It keeps the fixed
two-round strategy; it does not bargain indefinitely until a target is reached.

## Run the complete offline demo

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m sealed.service --sim-demo
```

This uses synthetic mail, extraction and transport, with the real grounding,
ledger, guard, qualifier, checkpoint and outbox implementations. It exercises a
$100 quote, a $90 counter, explicit simulated user approval and written booking
confirmation. It reopens the databases and checks that replay repeats no action.
The report is saved to gitignored `.sealed/coordinator_demo.json`. This command
constructs no live Gmail, Fo or Claude clients and sends no real messages.

## Live worker

The worker requires the Gmail, Fo and Anthropic configuration described in the
integration docs. Register a public `Job` JSON containing only fields from
`sealed.core.models.Job`, with `mode: "live"`. Keep the private ceiling out of that
file. Choose a distinct job/run ID for new outreach; registration does not import
earlier email history automatically.

Inspect one cycle without authorizing model requests or outreach:

```powershell
.\.venv\Scripts\python.exe -m sealed.service --job PATH_TO_PUBLIC_JOB.json --once
```

For a run whose model processing and outreach have been authorized:

```powershell
.\.venv\Scripts\python.exe -m sealed.service --job-id JOB --run-id RUN --restore-private --allow-model-requests --allow-dispatch
```

`--restore-private` prompts in an interactive terminal with input hidden. The
ceiling exists only in the process's private vault and is forgotten on shutdown.
After restart, restore it again before this run can progress. Never put it in a
command argument, public job JSON, event, checkpoint or Fo message.

Model authorization shares selected runs' seller reply text, receipt time and
public parsing context with Anthropic. Dispatch authorization enables guarded
outreach for the selected run. Calls additionally require `--allow-calls`.
Defaults authorize neither model requests nor outreach; external actions require
an explicit single-run selection. The earlier approval for one prepared $95
sample does not authorize processing other private replies.

Polling defaults to every 15 seconds. Use `--interval` (0.1–60 seconds), `--once`
for one cycle, or Ctrl+C for shutdown. Keep one account worker and one Fo state
file per account; an OS file lock rejects another worker using that state file.

## Processing and recovery

Each cycle polls first, applies staged replies, dispatches eligible intents and
then commits at most one due timer. A poll failure, unapproved parse, missing
private runtime, review-only identity or uncertain send prevents further affected
actions. Parser errors pause for review rather than repeatedly making paid calls.
Timer claims and transitions are saved together; dispatch claims survive crashes
and are never retried automatically when the send outcome is uncertain.

`Coordinator.approve()` requires the exact current proposal terms and queues the
booking only after explicit user approval. Pending replies invalidate approval.
`reject()` and durable `cancel()` are also available to application wiring.
Completion requires the seller's verified written confirmation. Live email sends
still require draft checks and an actual Gmail sent-copy audit.

## Current limits

The coordinator and CLI are implemented and covered by offline integration tests.
The live worker has not been activated against the existing teammate email test.
The [dashboard and job API](dashboard.md) now connect job intake, SSE, approval,
rejection, cancellation, private-state restoration and receipts to this service.
Uncertain Fo sends and paused jobs still require reviewed manual reconciliation;
automatic review/resume is not implemented here.
