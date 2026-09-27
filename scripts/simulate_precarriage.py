#!/usr/bin/env python3
"""Price every way from the plant to the port, and say which one wins.

    .venv/bin/python scripts/simulate_precarriage.py            # Unix
    .venv\\Scripts\\python scripts\\simulate_precarriage.py      # Windows

Prints, for each focus route's origin and gateway port, every chain in
config.example/fast.yaml → precarriage — truck, truck + barge, truck + rail —
with its cost per container and its time, in normal conditions and at the
Rhine level the board last recorded at Kaub. The lanes in lanes.yaml are
drawn on the winners; tests/test_precarriage.py fails if they drift apart.
Change a rate, run this, and redraw the lane if the winner changed.

Reads no customer data.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.clock import Clock  # noqa: E402
from engine.config import load_config  # noqa: E402
from engine.fast import precarriage as pc  # noqa: E402
from engine.ingest.observations import recorded_as_of  # noqa: E402
from engine.ingest.watergauge import assess_kaub  # noqa: E402
from engine.network.graph import Network  # noqa: E402


def main() -> int:
    config = load_config()
    network = Network(config)
    when = recorded_as_of()
    clock = Clock(datetime.fromisoformat(when)) if when else Clock.wall()
    readings, _report = assess_kaub(config, clock)
    kaub = next((r for r in readings if r.get("cost_multiplier", 1.0) > 1.0), None)
    surcharge = kaub["cost_multiplier"] if kaub else 1.0

    print(f"# Pre-carriage, priced — as of {clock.as_of:%Y-%m-%d %H:%M} UTC\n")
    if kaub:
        print(f"Kaub: {kaub['title']} — barge freight x{surcharge}.\n")
    for gateway in pc.gateways(config):
        normal = pc.compare(gateway, config, network)
        chosen = pc.choose(normal, config)
        low = pc.compare(gateway, config, network, surcharge) if kaub else []
        low_cost = {c.id: c.cost_chf for c in low}
        low_pick = pc.choose(low, config) if low else None
        origin = network.node(gateway["origin"]).name
        port = network.node(gateway["port"]).name
        print(f"## {origin} → {port} ({', '.join(gateway['lanes'])})\n")
        print("| chain | per container | time | at today's Kaub |")
        print("|---|---:|---:|---:|")
        for c in normal:
            mark = " **(drawn)**" if chosen and c.id == chosen.id else ""
            today = f"CHF {low_cost[c.id]:,.0f}" if c.id in low_cost else "—"
            print(f"| {c.id}{mark} | CHF {c.cost_chf:,.0f} | {c.hours:.0f} h | {today} |")
        if low_pick and chosen and low_pick.id != chosen.id:
            print(f"\nAt today's Kaub reading the cheapest is **{low_pick.id}**.")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
