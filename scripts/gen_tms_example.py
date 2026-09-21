"""Emit the worked RED alert example from Scenario C.

Generated from the real engine rather than written by hand, so the example an
integrator builds against is the payload they will actually receive. Run:

    python scripts/gen_tms_example.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import load_config
from engine.export import tms
from engine.score.severity import Level
from engine.taxonomy.resolve import Encounter, resolve

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "examples" / "tms_red_alert.json"

AS_OF = "2026-09-18T06:00:00+00:00"


def main() -> int:
    config = load_config()

    # Scenario C: weekend heavy-vehicle driving ban, ADR Class 3 solvent in a
    # chemical tanker, bound for a just-in-sequence automotive plant.
    resolution = resolve(config, Encounter(
        variable_id="GEO_REGULATORY",
        cargo_class="adr_class_3",
        asset_id="adr_tanker",
        channel_id="jit_jis",
        late_hours=52.0,
        consequence_cap=60_000.0,
        base_level=Level.YELLOW,
    )).as_dict()

    payload = tms.build_alert(
        board={
            "as_of": AS_OF,
            "config_version": config.version,
            "shipments_synthetic": True,
            "config_source": "config.example",
            "run_id": "EXAMPLE0",
        },
        route={
            "route_id": "LANE_EU_01",
            "name": "Düdingen → Stuttgart (road, ADR)",
            "directive": "Take action within 24–48 hours",
            "response": {
                "route_manager": {
                    "name": "P. Haas (synthetic)",
                    "role": "Intra-EU road & rail manager",
                    "email": "intraeu.surface@example-synthetic.invalid",
                },
                "escalation": {
                    "level": 3,
                    "notify": ["Supply Chain", "Procurement", "Customer Service"],
                },
            },
            "actions": [
                {
                    "label": "Depart Friday 18:00 ahead of the ban window",
                    "cost_chf": 780.0,
                    "avoids_chf": 31000.0,
                    "deadline_text": "Decide by Fri 18 Sep 14:00 UTC (8 h)",
                    "owner": "us",
                },
                {
                    "label": "Apply for a Sonntagsfahrverbot exemption (ADR hardship)",
                    "cost_chf": 240.0,
                    "avoids_chf": 31000.0,
                    "deadline_text": "Decide by Fri 18 Sep 10:00 UTC (4 h)",
                    "owner": "us",
                },
            ],
        },
        event={
            "event_id": "EVT-DRIVEBAN-0042",
            "title": "Weekend heavy-vehicle driving ban in force 00:00 Sun – 22:00 Sun",
            "severity": "moderate",
            "event_class": "geopolitical",
            "starts_at": "2026-09-20T00:00:00+00:00",
            "ends_at": "2026-09-20T22:00:00+00:00",
            "lat": 48.7758,
            "lon": 9.1829,
            "realized": False,
            "probability": 1.0,
            "probability_basis": "statutory and calendared; not an estimate",
            "source": "synthetic_authority",
            "source_tier": 1,
            "quote": (
                "Heavy goods vehicles above 7.5 t may not be operated on "
                "Sundays and public holidays between 00:00 and 22:00."
            ),
            "inferred": False,
            "active_variables": ["GEO_REGULATORY"],
        },
        resolution=resolution,
        shipment={
            "shipment_id": "SYN-0442",
            "customer": "Automotive OEM Süd (synthetic)",
            "origin_node": "SIKA_DUD",
            "destination_node": "DESTU",
            "eta": "2026-09-20T14:00:00+00:00",
            "otif_committed_date": "2026-09-20T16:00:00+00:00",
            "value_chf": 41250.0,
            "carrier": "CARR_ADR (synthetic stand-in)",
            "mode": "road",
            "asset_id": "adr_tanker",
            "cargo_class": "adr_class_3",
            "adr_class": "3",
        },
        run_id="EXAMPLE0",
    )

    problems = tms.audit_for_secrets(payload)
    if problems:
        print("REFUSING TO WRITE — credential-shaped content:", problems)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT}")
    print(f"  band={payload['alert']['band']} score={payload['alert']['score']}")
    print(f"  clashes={[c['rule'] for c in payload['assessment']['clashes_resolved']]}")
    print(f"  suppressed={[a['asset'] for a in payload['suppressed_actions']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
