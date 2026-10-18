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

Settings: copy `.env.example` to `.env.local` (gitignored). `VITE_DATA_SOURCE=mock` replays `../mock/*.json`; `live` talks to the backend at `VITE_API_BASE` (start it with `python -m app.run` in `backend/`). Only `VITE_*` values reach the browser, so never put secrets there.

Layout: `src/config.ts` (settings), `src/App.tsx` (placeholder that checks `/health`; replaced by the real screens), `vite.config.ts` (port 3000, `@mock` alias for `../mock`).

Build against `../docs/api.md` (v0.3). Until the backend is live:
- replay `../mock/events.json` as a fake SSE stream,
- use `../mock/ceiling.json` as the `GET /jobs/{id}/ceiling` response,
- use `../mock/discovery.json` as the `POST /dealers/discover` response.

Screens: job intake (car, ZIP, target, ceiling), dealer picker (discovered suggestions within 20 miles, or typed in; up to 5), target + ceiling display, live dealer view with a live/sim label on every event, trust counter, approval gate (echo the exact terms), receipt.

Ceiling rules for the dashboard:
- Fetch it only from `GET /jobs/{id}/ceiling`. Keep it in memory; never put it in a URL, `localStorage`, analytics, logs or any other request.
- Any "best quote vs ceiling" comparison is computed here and never sent anywhere.
- Show a one-line reminder: never tell Fo your ceiling (Fo remembers across chats).

Discovered dealers are suggestions: show distance and source links, label them "found by AI, check before contacting", and contact no one until the user picks them.
