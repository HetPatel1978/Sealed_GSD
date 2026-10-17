"""Builds mock/events.json by running the real engine in sim mode (backend/sealed/sim/run.py).

The mock is therefore exactly what the backend emits for the default car scenario: 3 stand-in dealers,
2 rounds, email + calls, a spoken price that is not firm, a flagged "as agreed, send a deposit" reply,
a rejected first proposal, and an approved, verified booking. Every offer is the target or null.

    backend/.venv/Scripts/python mock/build_mock.py && backend/.venv/Scripts/python mock/check_mock.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sealed.sim.run import run  # noqa: E402

events = run().log.events
out = Path(__file__).with_name("events.json")
out.write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")
print(f"wrote {len(events)} events to {out}")
