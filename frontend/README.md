# frontend/

The Sealed dashboard.

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
