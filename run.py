"""Thin entry point (BRIEF §9.2).

    python run.py demo                      one run at the default as-of
    python run.py demo --as-of 2026-09-18   pinned, reproducible
    python run.py inputs                    what is connected / stand-in / absent
    python run.py serve                     start the API + dashboard
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
            f"CHF {assessment.total_value_at_risk_chf:,.0f} at risk"
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


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the API and the dashboard it serves."""
    import subprocess

    print(f"  http://localhost:{args.port}")
    return subprocess.call(
        [
            sys.executable, "-m", "uvicorn", "api.main:app",
            "--host", args.host, "--port", str(args.port),
        ]
    )



def _default_host() -> str:
    """Loopback normally; every interface inside a forwarded container.

    A Codespace reaches the app through a port forwarder that lives outside
    the process namespace. Bound to 127.0.0.1 the server can be curled from
    the same terminal and still be invisible at the forwarded URL, which
    presents as "the page will not load" with a server that is plainly
    running — a confusing half-hour for anyone who has not hit it before.

    CODESPACES is set by GitHub; REMOTE_CONTAINERS by the Dev Containers
    extension locally. Explicit --host always wins.
    """
    import os

    in_container = os.environ.get("CODESPACES") or os.environ.get("REMOTE_CONTAINERS")
    return "0.0.0.0" if in_container else "127.0.0.1"  # noqa: S104


# =====================================================================
# Driver credentials
# =====================================================================
def cmd_driver(args) -> int:
    """Issue, list and revoke the credentials that can file field reports.

    A subcommand rather than a web form on purpose. Issuing a credential is
    an administrative act with a consequence — the holder can confirm a
    disruption and release a re-route — and it belongs with the person who
    has a shell on the machine, not behind a page anybody who reaches the
    server can find.
    """
    from datetime import UTC, datetime

    from engine.ingest import credentials as creds

    now = datetime.now(UTC)

    if args.action == "list":
        found = creds.drivers()
        if not found:
            print("No drivers registered.")
            print("The report endpoint is open, or uses RADAR_REPORT_TOKEN if set.")
            return 0
        print(f"{len(found)} driver(s):\n")
        for d in found:
            mark = "  " if d.active else "R "
            carrier = f"  {d.carrier}" if d.carrier else ""
            print(f"  {mark}{d.key_id}  {d.name}{carrier}")
            if not d.active:
                print(f"       revoked {d.revoked_at}")
        print("\nR = revoked. Per-driver credentials are IN FORCE while any "
              "driver is active;\nthe shared RADAR_REPORT_TOKEN is not "
              "sufficient on its own once they are.")
        return 0

    if args.action == "revoke":
        if not args.key:
            print("Which one? Pass the key id from `run.py driver list`.", file=sys.stderr)
            return 2
        try:
            gone = creds.revoke(args.key, now)
        except creds.CredentialError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Revoked {gone.key_id} ({gone.name}). Their reports stay in the log —")
        print("it is append-only, and what they filed while trusted still happened.")
        return 0

    # add
    if not args.name:
        print("A driver needs a name: run.py driver add \"Hans Meier\"", file=sys.stderr)
        return 2
    try:
        driver, token = creds.issue(args.name, args.carrier, now)
    except creds.CredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    base = args.base_url.rstrip("/")
    print(f"\n  {driver.name}" + (f"  ({driver.carrier})" if driver.carrier else ""))
    print(f"  key   {driver.key_id}")
    print(f"\n  TOKEN {token}")
    print("\n  Hand them this link once, on the phone that will use it:")
    print(f"    {base}/driver?k={token}")
    print("\n  The app keeps it and strips it from the address bar. The token is")
    print("  shown HERE AND NOWHERE ELSE — only its hash is stored, so it cannot")
    print("  be looked up later. Lost means revoke and issue a new one.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (
        ("demo", cmd_demo),
        ("inputs", cmd_inputs),
        ("serve", cmd_serve),
    ):
        p = sub.add_parser(name)
        p.add_argument("--as-of", default=DEFAULT_AS_OF)
        p.add_argument("--shipments", type=int, default=150)
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--host", default=_default_host())
        p.add_argument("--port", type=int, default=8000)
        p.set_defaults(handler=handler)

    driver = sub.add_parser("driver", help="issue and revoke driver credentials")
    driver.add_argument("action", choices=["add", "list", "revoke"])
    driver.add_argument("name", nargs="?", help="the driver's name, for `add`")
    driver.add_argument("--carrier", default=None)
    driver.add_argument("--key", default=None, help="key id, for `revoke`")
    driver.add_argument("--base-url", default="http://localhost:8000")
    driver.set_defaults(handler=cmd_driver)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
