#!/usr/bin/env python3
"""Every burst of small orders in Sika's order book, and what followed it.

    .venv/bin/python scripts/check_order_bursts.py [--flow CH_US]

Reads config/orders_daily.yaml (written by scripts/import_sika_flows.py; it
stays on this machine) and prints, per flow, each day the burst rule fires
(desk.yaml -> order_bursts) with the known public event that followed it
within two weeks, if any. The check that the sign comes BEFORE a crisis,
and how often it fires without one: a burst also follows year-end ordering,
holidays and campaigns, so it is a reason to look, not a verdict.

Nothing is written and nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import focus as focus_mod  # noqa: E402
from engine.config import CUSTOMER_DIR, load_config  # noqa: E402
from engine.ingest import bursts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", help="one flow only, e.g. CH_US")
    parser.add_argument("--all", action="store_true", help="every flow, not only the five focus flows")
    args = parser.parse_args()

    path = CUSTOMER_DIR / bursts.ORDERS_FILE
    if not path.exists():
        print(f"no {path.relative_to(ROOT)}: run scripts/import_sika_flows.py on the export first")
        return 1
    config = load_config()
    history = bursts.read_file(path)
    focus = sorted({r.sika_flow for r in focus_mod.focus_routes(config).values() if r.sika_flow})
    wanted = [args.flow] if args.flow else (sorted(history) if args.all else focus)
    found = bursts.backtest({f: history[f] for f in wanted if f in history},
                            bursts.rule(config), bursts.known_events(config))
    print(f"rule: {bursts.rule(config)}")
    for flow in wanted:
        rows = [b for b in found if b["flow"] == flow]
        days = len(history.get(flow, {}))
        print(f"\n{flow}: {len(rows)} burst(s) over {days} order days")
        for b in rows:
            after = b["followed_by"]
            note = (f"  <- {after['days_later']} day(s) before: {after['what']}" if after else "")
            print(f"  {b['day']}  {b['orders']:>3} orders (usual {b['usual']:g}), "
                  f"{b['small_share']:.0%} smaller than usual{note}")
    matched = sum(1 for b in found if b["followed_by"])
    print(f"\n{len(found)} burst(s); {matched} followed by a listed event within two weeks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
