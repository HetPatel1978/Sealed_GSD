# Sealed

Sealed is a negotiation agent built on Wajo's Fo. You describe a job (what, how many, by when), set a target price and a secret ceiling, and list a few sellers. Fo emails each seller from your Wajo account, Sealed reads their replies from your inbox, counters using only true facts (real written quotes, never invented leverage), and asks you to approve the winning deal before anything is booked. The ceiling never leaves Sealed's private core: it is never sent to Fo, to any seller, to any LLM, or to the frontend, and every outgoing message is checked before and after it is sent.

## Layout

```
backend/   FastAPI app (Het)
frontend/  Renu's dashboard, design is hers
docs/      api.md (API + event contract), fo_tools.md (Fo MCP findings), architecture diagram
mock/      events.json: one full simulated run as an event stream; check_mock.py validates it
spikes/    throwaway experiments, not product code
```

## Run the backend

Python 3.11+.

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
# http://localhost:8000/health
pytest
```

Credentials (Fo OAuth token, Gmail app password, Anthropic key) load from environment variables only. Copy `.env.example` to `.env` and fill it in locally. `.env` and token files are gitignored.

## Frontend

`frontend/` is Renu's. Build against `docs/api.md` and replay `mock/events.json` until the backend streams real events.

## Mock data

```powershell
python mock/build_mock.py   # regenerate mock/events.json
python mock/check_mock.py   # fails if anything looks like a ceiling, or the numbers don't add up
```
