# Sealed

Sealed is a negotiation agent built on Wajo's Fo. You describe a job (what, how many, by when), set a target price and a secret ceiling, and either list a few sellers or let Sealed find dealers within 20 miles of your ZIP and pick from its suggestions. Fo emails each seller from your Wajo account, Sealed reads their replies from your inbox, counters using only true facts (real written quotes, never invented leverage), and asks you to approve the winning deal before anything is booked. The ceiling is never sent to Fo, to any seller, or to any LLM, and every outgoing message is checked before and after it is sent. You can see your own target and ceiling on the dashboard; the ceiling reaches it through one private endpoint and never appears in the event stream.

## Design

**Current design: [`docs/architecture.md`](docs/architecture.md)** ("sealed envelope": code decides every fact, Fo does the outreach by email and phone, only the qualifier sees the ceiling). Build plan: [`docs/plan.md`](docs/plan.md); decisions: [`docs/decisions.md`](docs/decisions.md); threats: [`docs/threat_model.md`](docs/threat_model.md); Fo MCP facts: [`docs/research/fo_mcp.md`](docs/research/fo_mcp.md).

`docs/sealed_module_io_flow.png` is **superseded** by `docs/architecture.md` (kept for history). `docs/fo_tools.md` is also superseded by `docs/research/fo_mcp.md`.

## Layout

```
backend/   FastAPI app
frontend/  dashboard
docs/      architecture.md (current design), api.md, plan.md, decisions.md, threat_model.md, research/
mock/      events.json: one full simulated run as an event stream; check_mock.py validates it;
           ceiling.json + discovery.json: sample responses for the ceiling and discovery endpoints
spikes/    throwaway experiments, not product code
```

## Run the backend

Python 3.11+. One venv at the repo root (`.venv/`, gitignored) covers the backend, `spikes/` and `mock/`. `requirements-dev.txt` pulls in `requirements.txt` plus test and spike dependencies.

macOS / Linux:

```bash
python3.11 -m venv .venv            # any python 3.11+ (check with python3 --version)
source .venv/bin/activate
pip install -r backend/requirements-dev.txt
cd backend
python -m app.run          # binds to 127.0.0.1:8000 only (refuses any other host)
# http://localhost:8000/health
pytest
```

Windows (PowerShell):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements-dev.txt
cd backend
python -m app.run
pytest
```

The API has no login (and `GET /jobs/{id}/ceiling` returns the ceiling), so it is **local only**: it binds to 127.0.0.1, refuses requests from any other machine (403) or with a non-local `Host` header (400), and CORS allows exactly one localhost dashboard origin (`SEALED_DASHBOARD_ORIGIN`, default `http://localhost:3000`). A bad setting stops startup. See `backend/app/security.py`.

Credentials (Fo OAuth token, Gmail app password, Anthropic key) load from environment variables only. Copy `.env.example` to `.env` and fill it in locally. `.env` and token files are gitignored.

## Frontend

Build against `docs/api.md`. Until the backend streams real events, replay `mock/events.json` and use `mock/ceiling.json` and `mock/discovery.json` as the responses of `GET /jobs/{id}/ceiling` and `POST /dealers/discover`.

## Mock data

```powershell
python mock/build_mock.py   # regenerate mock/events.json
python mock/check_mock.py   # fails if anything looks like a ceiling, or the numbers don't add up
```
