# Dashboard and job API

The dashboard now uses the coordinator through FastAPI, including job creation,
status, SSE activity, exact-term approval/rejection, cancellation and receipts.
Simulations use the same durable engine and approval gate with synthetic mail,
extraction and transport. They construct no live integration clients.

## Start locally

In one PowerShell terminal, using the existing backend virtual environment:

```powershell
cd backend
$env:SEALED_MODE = 'sim'
.\.venv\Scripts\python.exe -m app.run
```

In a second terminal:

```powershell
cd frontend
npm.cmd run dev
```

Open `http://localhost:3000`. The frontend defaults to the backend API at
`http://127.0.0.1:8000`. If `frontend/.env.local` already sets
`VITE_DATA_SOURCE=mock`, change that to `live` and restart Vite. Frontend `live`
means use the API; the API's `SEALED_MODE=sim` still makes no real requests.
`mock` is a read-only preview of the recorded event fixture.

### If `python -m app.run` fails

Run it from `backend/`, where the `app` package lives. From the repository root,
plain `python -m app.run` fails with `No module named 'app'`.

Use the project interpreter: `.\.venv\Scripts\python.exe -m app.run` from
`backend/`. Plain `python` can select Miniconda instead of this environment and
fail on missing dependencies such as `email_reply_parser`. `Get-Command python`
shows which interpreter PowerShell selected. The explicit interpreter command
works without activating a PowerShell script or changing the execution policy.

Enter the public specification and ZIP, then click **Find dealers** and select candidates, or add sellers manually. Simulation uses synthetic candidates; live research requires its own permission checkbox. See [dealer discovery](dealer_discovery.md). Create a job and enter a private ceiling above its public target.
Creation contacts no one. Click **Start simulation**, then **Advance to next
decision**. Review and approve the proposed terms. The background worker releases
the synthetic written confirmation; **Simulate written confirmation** can also
advance it immediately. The sample starts above the target and returns a quote
at the target; this is synthetic test behavior, not a promise about real sellers.

## Live jobs

Set `SEALED_MODE=live` in the backend process and supply the integration credentials
locally. On the dashboard, starting each live job requires explicit permission for
seller reply text to be processed by Anthropic and for guarded Fo email outreach.
Calls require additional permission; a pending scheduled call can be authorized
from the dashboard. No live job or additional private email export was activated
while implementing this API.

Run only one API process. Its lifespan owns a single executor thread for the
account, a durable job worker and the SQLite connections. It also acquires the
same Fo worker lock as `python -m sealed.service`; do not run both live workers
against one Fo account. Simulated and live API data live in separate gitignored
`.sealed/api_sim/` and `.sealed/api_live/` directories by default. Set
`SEALED_API_DATA_DIR` to override that directory. Earlier CLI/smoke history is not
automatically imported as a new dashboard job.

## Private state and browser access

The ceiling is accepted only by creation/restoration and returned only by
`GET /jobs/{id}/ceiling`. It is excluded from public job data, events, SQLite
checkpoints, logs and validation errors. The dashboard keeps it in memory and
does not place it in browser storage. After a backend restart, persisted jobs
remain visible but require private-ceiling restoration before further actions.

The API remains local only: loopback binding and clients, trusted local Host,
exact dashboard Origin checks, JSON-only POSTs and `no-store` responses. An
explicitly permitted dashboard Origin works even when the browser classifies
`localhost` to `127.0.0.1` as cross-site. Foreign and opaque Origins are refused.

## Implemented API additions

- `GET /jobs`: list saved public jobs for this account.
- `POST /jobs/{id}/private-runtime`: `{ "ceiling": NUMBER }` restores missing
  private state; it does not overwrite an existing ceiling or echo its value.
- `POST /jobs/{id}/allow-calls`: `{ "allow_calls": true }` adds call permission
  to an already authorized running live job.
- `POST /jobs/{id}/start`: accepts `allow_model_requests`, `allow_dispatch` and
  optional `allow_calls`. Both model and dispatch permissions are required live.
- `GET /jobs/{id}/events`: SSE replay from `Last-Event-ID` or `last_event_id`,
  followed by updates and 15-second heartbeats. `follow=false` returns just the
  current replay for inspection. Invalid/future cursors return `422`.
- `POST /sim/advance`: steps coordinator cycles and advances the virtual clock
  to due timers. `steps=-1` runs up to the next approval/review gate or completion;
  it never approves a proposal automatically. Live mode returns `404`.

Cancellation stops queued outreach and does not recall an in-flight send. It
does not send cancellation notes to sellers. Rejection/cancellation free text is
not copied into public events or outreach, since users might enter private data.

## Validation and remaining work

The backend tests cover the API negotiation, approval conflicts, ceiling isolation,
SSE replay, restart restoration, cancellation, background progression and live
authorization. The frontend passes its TypeScript production build and lint.
A Chrome browser check completed the simulated $99 → $90 → user approval →
written confirmation flow with 32 SSE events and no browser-storage entries.

Fo discovery and selection are implemented in the intake form, with cached progress and backend provenance checks. Paused jobs and uncertain sends still require reviewed reconciliation;
the dashboard does not force-resume them. New live end-to-end acceptance remains
separate from the offline/API/browser checks. The strategy remains two rounds.
