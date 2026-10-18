# frontend/

The Sealed dashboard: React + TypeScript, built with Vite.

## Run it

Needs Node.js 20+ (`brew install node` on macOS).

```bash
cd frontend
npm install        # once, and after package.json changes
npm run dev        # http://localhost:3000 (fixed port: the backend only accepts this origin)
npm run build      # type-check + production build into dist/
npm run lint
```

Settings: copy `.env.example` to `.env.local` (gitignored). `VITE_DATA_SOURCE=live` (default) talks to the backend; `mock` shows the read-only recorded activity preview. The API defaults to simulation, so using the API does not by itself authorize real outreach. The backend is at `VITE_API_BASE` (start it with `python -m app.run` in `backend/`). Only `VITE_*` values reach the browser, so never put secrets there.

Layout: `src/config.ts` (settings), `src/App.tsx` (job intake, comparison, event stream, approvals and receipt), `src/api.ts` (typed API client), `vite.config.ts` (port 3000, `@mock` alias for `../mock`).

See [dashboard startup and limits](../docs/dashboard.md) and the [API contract](../docs/api.md).
The dashboard creates jobs, streams coordinator events through SSE, displays written
quotes and trust checks, echoes exact proposal terms on approval, and shows the
final evidence receipt. It can restore a private ceiling after backend restart.
Live job start requires explicit reply-processing and outreach permission; calls
require a separate permission. The simulation advance action stops for approval.

The **Find dealers** panel supports Fo-first public research, progress polling,
checked contacts and selection before job creation. Simulation uses synthetic
candidates. Live research has its own permission checkbox. See
[dealer discovery and limitations](../docs/dealer_discovery.md).

Ceiling rules for the dashboard:
- Fetch it only from `GET /jobs/{id}/ceiling`. Keep it in memory; never put it in a URL, `localStorage`, analytics, logs or any other request.
- Any "best quote vs ceiling" comparison is computed here and never sent anywhere.
- Show a one-line reminder: never tell Fo your ceiling (Fo remembers across chats).

Discovered dealers are suggestions: show distance and source links, label them "found by AI, check before contacting", and contact no one until the user picks them.
