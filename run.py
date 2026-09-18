"""Thin entry point (BRIEF §9.2).

    python run.py demo                      one run at the default as-of
    python run.py demo --as-of 2026-09-18   pinned, reproducible
    python run.py inputs                    what is connected / stand-in / absent
    python run.py dashboard                 launch the Streamlit UI
"""

from __future__ import annotations

import argparse
import sys

from engine.clock import Clock
from engine.config import load_config
from engine.ingest.observations import FeedStatus
from engine.pipeline import RunOptions, run

# The demo runs at a pinned instant so that what you rehearse is what happens
# on stage. Nothing in engine/ reads the wall clock.
DEFAULT_AS_OF = "2026-09-18T06:00:00+00:00"


def cmd_demo(args: argparse.Namespace) -> int:
    clock = Clock.at(args.as_of)
    config = load_config()
    context = run(
        clock=clock,
        config=config,
        options=RunOptions(shipment_count=args.shipments, seed=args.seed),
    )
    result = context.result

    print()
    print("=" * 72)
    print(f"  SUPPLY CHAIN RISK RADAR — as of {clock}")
    print(f"  config {result.config_version} · {len(config.variables)} risk variables")
    print("=" * 72)

    verdict = result.convene
    print()
    print(f"  POSTURE: {verdict.posture.value.upper()}")
    print(f"  {verdict.headline}")
    if not verdict.rule_agreed:
        print("  (convene rule not yet agreed with the planning team)")
    print()

    f = result.funnel
    print("  FUNNEL (measured, not asserted)")
    print(f"    raw observations      {f.raw_observations:>6}")
    print(f"    after geographic      {f.after_geographic:>6}")
    print(f"    after type            {f.after_type:>6}")
    print(f"    after temporal        {f.after_temporal:>6}")
    print(f"    after resolution      {f.after_resolution:>6}")
    print(f"    reasoned              {f.reasoned:>6}")
    print(f"    gate hits             {f.gated_hits:>6}")
    print(f"    shipments touched     {f.shipments_touched:>6} of {result.shipments_total}")
    print()

    print(f"  EVENTS THAT TOUCH YOUR FREIGHT: {result.events_total}")
    print()
    for assessment in result.assessments:
        event = assessment.event
        p = (
            f"P={event.probability:.2f}"
            if event.probability_known
            else "P unsourced"
        )
        print(f"  ─ {event.title}")
        print(
            f"    {event.severity.value} · {p} · "
            f"{assessment.shipments_affected} shipments · "
            f"CHF {assessment.total_value_at_risk_chf:,.0f} at risk · "
            f"CHF {assessment.total_value_of_acting_chf:,.0f} recoverable"
        )
        top = assessment.shipment_risks[0]
        # BRIEF §5.4: recommend the action when value of acting > 0. A negative
        # value means the cheapest option costs more than it saves — printing it
        # as a recommendation would be advising the planner to lose money.
        if top.best_action is not None and top.value_of_acting_chf > 0:
            from engine.score.impact import explain
            from engine.score.leadtime import deadline_text

            print(
                "    → "
                + explain(
                    top.do_nothing.expected_loss_chf,
                    top.act_outcome.expected_loss_chf if top.act_outcome else 0.0,
                    top.best_action.cost_chf,
                    top.best_action.label,
                    deadline_text(clock, top.decision_deadline),
                )
            )
        elif top.actionability == "too_late":
            print("    → too late to reroute — notify the customer and re-agree the date")
        else:
            print("    → monitor: no action currently saves more than it costs")
        print()

    if result.decay_curve:
        print("  OPTION DECAY")
        for point in result.decay_curve[:: max(1, len(result.decay_curve) // 6)]:
            bar = "█" * int(
                28 * point.recoverable_chf / max(1.0, result.decay_curve[0].recoverable_chf)
            )
            print(
                f"    +{point.hours_from_now:>4.0f} h  "
                f"CHF {point.recoverable_chf:>10,.0f}  {bar}"
            )
        print()

    return 0


def cmd_inputs(args: argparse.Namespace) -> int:
    clock = Clock.at(args.as_of)
    context = run(
        clock=clock,
        config=load_config(),
        options=RunOptions(shipment_count=args.shipments, seed=args.seed),
    )

    order = {FeedStatus.CONNECTED: 0, FeedStatus.FIXTURE: 1, FeedStatus.ABSENT: 2}
    mark = {
        FeedStatus.CONNECTED: "[connected]",
        FeedStatus.FIXTURE: "[stand-in ]",
        FeedStatus.ABSENT: "[absent   ]",
    }

    print()
    print("  INPUTS — what is real, what is standing in, what is missing")
    print("  " + "-" * 68)
    for report in sorted(context.reports, key=lambda r: order[r.status]):
        print(f"  {mark[report.status]}  {report.label}")
        print(f"                {report.detail}")
        if report.status is not FeedStatus.CONNECTED and report.unlocks_if_connected:
            print(f"                ↳ would unlock: {report.unlocks_if_connected}")
        print()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import subprocess

    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", "dashboard/app.py"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (
        ("demo", cmd_demo),
        ("inputs", cmd_inputs),
        ("dashboard", cmd_dashboard),
    ):
        p = sub.add_parser(name)
        p.add_argument("--as-of", default=DEFAULT_AS_OF)
        p.add_argument("--shipments", type=int, default=150)
        p.add_argument("--seed", type=int, default=None)
        p.set_defaults(handler=handler)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
