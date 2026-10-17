import ast
from dataclasses import replace
import json
import subprocess
import sys
from pathlib import Path

from sealed.core.numbers import numbers_in
from sealed.sim.run import default_dealers, default_job, run

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
CEILING = 32000.0


def all_strings_and_numbers(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from all_strings_and_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_strings_and_numbers(v)
    else:
        yield obj


def test_full_sim_run_completes_with_the_expected_story():
    eng = run(ceiling=CEILING)
    ev = eng.log.events
    assert eng.state == "COMPLETED"
    props = [e for e in ev if e["type"] == "proposal.created"]
    assert [p["vendor_id"] for p in props] == ["v_harbor", "v_harbor"]       # flagged Summit never proposed
    assert [p["data"]["price_total"] for p in props] == [30980.0, 30450.0]
    done = ev[-1]["data"]
    assert done["best_quote"] == 30450.0 and done["first_quote"] == 31240.0
    assert done["trust"]["budget_revealed_count"] == 0 and done["trust"]["commitments_without_approval"] == 0
    assert done["trust"]["suspicious_instructions_flagged"] == 1


def test_ceiling_never_appears_in_any_event_or_message():
    ev = run(ceiling=CEILING).log.events
    for item in all_strings_and_numbers(ev):
        if isinstance(item, str):
            assert not any(abs(n - CEILING) <= 0.02 * CEILING for n in numbers_in(item)), item
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            assert abs(item - CEILING) > 0.02 * CEILING, item


def test_offers_are_target_or_null_and_spoken_prices_never_firm():
    ev = run(ceiling=CEILING).log.events
    target = ev[0]["data"]["target"]
    for e in ev:
        if e["type"] in ("strategy.decided", "message.sent"):
            assert e["data"].get("offer") in (None, target)
        if e["type"] == "quote.parsed" and e["data"]["source"] == "call":
            assert e["data"]["firm"] is False


def _plan(eng):
    """The outgoing round plan: every message actually sent, with timing and exact text."""
    return [(e["ts"], e["vendor_id"], e["data"]["kind"], e["data"]["channel"], e["data"]["body"])
            for e in eng.log.events if e["type"] == "message.sent"]


def test_true_quote_within_2pct_of_ceiling_is_cited_and_plan_is_unchanged():
    # Ceiling 31,300: Summit's written $30,690 (-1.9%) and Harbor's $30,980 (-1.0%) sit inside the band.
    near, far = run(ceiling=31300.0), run(ceiling=40000.0)
    assert _plan(near) == _plan(far)
    bodies = " ".join(b for *_, b in _plan(near))
    assert "$30,690" in bodies
    assert not any(e["type"] == "guard.checked" and not e["data"]["passed"] for e in near.log.events)
    assert [e["type"] for e in near.log.events] == [e["type"] for e in far.log.events]


def test_target_within_2pct_of_ceiling_counter_is_sent_and_plan_is_unchanged():
    job = replace(default_job(), target=30600.0)            # below Summit's $30,690, so Summit is countered
    near, far = run(job=job, ceiling=31000.0), run(job=job, ceiling=40000.0)
    counters = [e for e in near.log.events if e["type"] == "message.sent" and e["data"]["kind"] == "counter"]
    assert counters and counters[0]["data"]["offer"] == 30600.0 and "$30,600" in counters[0]["data"]["body"]
    assert _plan(near) == _plan(far)


def test_needs_human_when_no_quote_is_within_the_ceiling():
    eng = run(ceiling=25000.0)
    assert eng.state == "NEEDS_HUMAN"
    nh = [e for e in eng.log.events if e["type"] == "job.needs_human" and e["data"]["blocking"]]
    assert nh and nh[-1]["data"]["reason_code"] == "no_qualifying_quote" and "closest" in nh[-1]["data"]


def test_mock_checker_accepts_the_sim_stream(tmp_path):
    out = tmp_path / "events.json"
    out.write_text(json.dumps(run(ceiling=CEILING).log.events))
    r = subprocess.run([sys.executable, str(REPO / "mock" / "check_mock.py"), str(out), "--secret", str(CEILING)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_only_the_private_zone_and_the_wiring_import_private_modules():
    allowed = {BACKEND / "sealed" / "sim" / "run.py"}
    for py in (BACKEND / "sealed").rglob("*.py"):
        if "private" in py.parts or py in allowed:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.startswith("sealed.private") for n in names), f"{py} imports {names}"


def test_job_model_has_no_ceiling_field():
    job = default_job()
    assert not any("ceil" in f or "budget" in f for f in vars(job))
    assert default_dealers(job)
