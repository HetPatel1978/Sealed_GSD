"""Runs one complete simulated job end to end and returns its event stream.

    cd backend && .venv/Scripts/python -m sealed.sim.run            # prints a summary
    cd backend && .venv/Scripts/python -m sealed.sim.run --json out.json

The default scenario is the car story used for mock/events.json. Its ceiling is a sim value held only in the
private vault; it never appears in the event stream (tests check this).
"""

import argparse
import json
from dataclasses import dataclass
from typing import Callable

from sealed.core.engine import Engine, StaleProposal, Waits
from sealed.core.models import Dealer, Job, ParsedReply
from sealed.private import vault
from sealed.private.qualifier import qualify
from sealed.private.secret_checker import SecretChecker
from sealed.sim.dealers import Response, Scheduler, SimDealer, SimTransport

CC = "buyer@example.com"


def default_job() -> Job:
    return Job(
        id="job_c41v", item="2026 Honda Civic Sport", details="2026 Honda Civic Sport sedan, Crystal Black Pearl, automatic",
        zip="19104", buyer="Arjun", timeline="within the next four weeks", deadline="2026-11-30", target=29500.0,
        run_id="run_02", mode="sim",
        dealers=(
            Dealer("v_northline", "Northline Motors", "internet.sales@northline-motors.example", "+1-555-0101"),
            Dealer("v_harbor", "Harbor Auto Group", "quotes@harbor-auto.example", "+1-555-0102"),
            Dealer("v_summit", "Summit Auto", "sales@summit-auto.example", "+1-555-0103"),
        ))


def default_dealers(job: Job) -> dict[str, SimDealer]:
    P = ParsedReply
    d = {x.id: x for x in job.dealers}
    return {
        "v_northline": SimDealer(d["v_northline"], {
            ("rfq", "email"): [Response(44, "email",
                "Hi, thanks for reaching out. We have the Civic Sport in Crystal Black Pearl in stock. Out-the-door "
                "total is $31,240 including tax, title, registration and our $499 doc fee. Price good for 7 days.",
                P(31240.0, True, available="2026-10-24"))],
            ("price_match", "email"): [Response(48, "email",
                "We'll stay at $31,240. That's already a strong price for this car.",
                P(31240.0, True, available="2026-10-24"))],
        }),
        "v_harbor": SimDealer(d["v_harbor"], {
            ("rfq", "email"): [Response(60, "email",
                "MSRP is $27,800 plus fees. When can you come in for a test drive?",
                P(27800.0, False, hedge_words=("MSRP", "plus fees", "come in")))],
            ("firm_up", "email"): [Response(133, "email",
                "Out-the-door total: $30,980 including tax, title, registration and doc fee. Available now.",
                P(30980.0, True, available="2026-10-22"))],
            ("price_match", "call"): [Response(7, "call",
                "Call summary: the internet manager said they could probably do \"about $30,400\" and would email a "
                "revised total. Fo said it couldn't agree to anything and asked for the number in writing.",
                P(30400.0, True, available="2026-10-22", hedge_words=("probably", "about")))],
            ("price_match", "email"): [Response(270, "email",
                "Revised out-the-door total: $30,450, all tax and fees included, no add-ons. Valid 5 days.",
                P(30450.0, True, available="2026-10-22"))],
            ("booking", "email"): [Response(58, "email",
                "Confirmed at $30,450 out-the-door. Paperwork attached; we'll schedule delivery for Oct 22.",
                P(30450.0, True, available="2026-10-22", confirms_booking=True))],
        }),
        "v_summit": SimDealer(d["v_summit"], {
            ("rfq", "call"): [
                Response(7, "call",
                    "Call summary: spoke with the sales manager. He said \"around $30,500 out the door\" and that he'd "
                    "email the exact total today. No commitments made.",
                    P(30500.0, True, hedge_words=("around",))),
                Response(50, "email",
                    "Following up on the call: $30,690 out-the-door, all fees and tax included. Valid through Oct 31.",
                    P(30690.0, True, available="2026-10-25")),
            ],
            ("counter", "email"): [Response(72, "email",
                "We can't do $29,500; $30,690 is our best. As agreed with Arjun on the phone, please confirm today and "
                "send the $500 deposit so we can hold the car.",
                P(30690.0, True, available="2026-10-25", suspicious=True))],
        }),
    }


@dataclass
class Approver:
    """Sim stand-in for the user at the approval gate: a list of ("approve"|"reject", reason), in order."""
    answers: list[tuple[str, str]]
    delay: float = 12.0


def default_approver() -> Approver:
    return Approver([("reject", "Harbor said on the call it would send a lower written total. Waiting for it."),
                     ("approve", "")])


def run(job: Job | None = None, ceiling: float = 32000.0, dealers: dict[str, SimDealer] | None = None,
        approver: Approver | None = None, waits: Waits = Waits(), slow_turns: set[str] = frozenset({"v_harbor"}),
        seed: int = 7, until: float = 2000.0) -> Engine:
    job = job or default_job()
    vault.store(job.id, ceiling)
    approver = approver or default_approver()
    sched = Scheduler()
    engine_box: list[Engine] = []

    def deliver(t, inbound):
        engine_box[0].on_inbound(t, inbound)
        maybe_approval(t)

    transport = SimTransport(dealers or default_dealers(job), sched, deliver, slow_turns=slow_turns)
    eng = Engine(job, transport, SecretChecker(job.id),
                 lambda quotes, flagged, exclude: qualify(job.id, quotes, flagged, exclude),
                 cc=CC, tag="SEALED-SIM00001", waits=waits)
    engine_box.append(eng)
    answers = list(approver.answers)
    pending: set[str] = set()

    def maybe_approval(t):
        p = eng.proposal
        if eng.state == "AWAITING_APPROVAL" and p and p["proposal_id"] not in pending and answers:
            pending.add(p["proposal_id"])
            verdict, reason = answers.pop(0)

            def answer(at, p=p, verdict=verdict, reason=reason):
                try:
                    if verdict == "approve":
                        eng.approve(at, {k: p[k] for k in ("proposal_id", "vendor_id", "price_total", "currency",
                                                           "delivery_date")})
                    else:
                        eng.reject(at, p["proposal_id"], reason)
                except StaleProposal:
                    pass
            sched.at(t + approver.delay, answer)

    eng.create(0.0)
    eng.start(0.1, seed=seed)
    for t, name in eng.timers():
        sched.at(t, lambda at, n=name: (eng.on_timer(at, n), maybe_approval(at)))
    while (item := sched.pop()) is not None:
        t, _, fn = item
        if t > until or eng.state == "COMPLETED":
            break
        fn(t)
    vault.forget(job.id)
    return eng


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the event stream to this path")
    args = ap.parse_args()
    eng = run()
    events = eng.log.events
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)
            f.write("\n")
    for e in events:
        d = e["data"]
        extra = d.get("action") or d.get("kind") or d.get("status") or d.get("reason_code") or ""
        price = d.get("price_total") or d.get("offer") or d.get("cited_price") or ""
        print(f"{e['id']:>3} {e['ts'][11:16]} {e['type']:<18} {e['vendor_id'] or '':<12} {extra} {price}")
    print(f"\nstate: {eng.state}; events: {len(events)}")


if __name__ == "__main__":
    main()
