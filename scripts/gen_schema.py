"""Regenerate schemas/event_radar_taxonomy.json from the config.

Generated rather than hand-written so the enums cannot drift from what the
engine actually loads. A schema that disagrees with the runtime is worse than
no schema: integrators build against it and fail in production.

    python scripts/gen_schema.py            # writes the file
    python scripts/gen_schema.py --check    # CI mode: fails if stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "schemas" / "event_radar_taxonomy.json"


def build() -> dict:
    tx = yaml.safe_load((ROOT / "config.example" / "taxonomy.yaml").read_text(encoding="utf-8"))
    variables = yaml.safe_load(
        (ROOT / "config.example" / "variables.yaml").read_text(encoding="utf-8")
    )["variables"]

    def enum(names, extra="unmapped"):
        return sorted(list(names) + [extra])

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.invalid/schemas/event_radar_taxonomy.json",
        "title": "Supply Chain Outbound Risk Radar — four-layer taxonomy",
        "description": (
            "Generated from config.example/taxonomy.yaml and variables.yaml by "
            "scripts/gen_schema.py. Do not hand-edit: regenerate. The enums "
            "below are therefore guaranteed to match what the engine loads.\n\n"
            "EVERY LAYER CARRIES AN UNMAPPED FALLBACK. A taxonomy that cannot "
            "express 'we saw something we have no box for' forces the caller "
            "to pick the nearest wrong box, and the wrong box is "
            "indistinguishable from a correct one downstream. The fallback "
            "keeps an unmapped observation visible as unmapped."
        ),
        "type": "object",
        "required": ["layer1_event", "layer2_asset", "layer3_channel", "layer4_cargo"],
        "additionalProperties": False,
        "$defs": {
            "unmapped": {
                "type": "object",
                "description": (
                    "The escape hatch, present on every layer. When `id` is "
                    "'unmapped', `raw_label` carries what was actually "
                    "observed. Consumers MUST treat it as unknown, never as a "
                    "default member of the enum."
                ),
                "required": ["raw_label", "reason"],
                "additionalProperties": False,
                "properties": {
                    "raw_label": {"type": "string", "minLength": 1},
                    "reason": {
                        "type": "string", "minLength": 1,
                        "description": (
                            "Why it did not map. Not optional: an unexplained "
                            "gap is indistinguishable from a bug."
                        ),
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "nearest_candidates": {
                        "type": "array", "items": {"type": "string"}, "maxItems": 5,
                        "description": (
                            "What it nearly matched. Advisory only — the "
                            "system must not silently pick one."
                        ),
                    },
                },
            },
            "pathway": {
                "type": "string",
                "enum": ["delay", "damage", "both", "none"],
                "description": (
                    "How the event threatens the freight. `delay` applies to "
                    "everything on the corridor; `damage` only where Layer 4 "
                    "opens the gate. Splitting these is what lets a severe "
                    "event against invulnerable cargo produce a calm, correct "
                    "answer instead of a false Red."
                ),
            },
            "probability": {
                "description": (
                    "null means the probability genuinely cannot be sourced. "
                    "It MUST NOT be replaced with 0.5 or any imputed value; "
                    "consumers render it in a separate band outside the axis."
                ),
                "oneOf": [
                    {"type": "number", "minimum": 0, "maximum": 1},
                    {"type": "null"},
                ],
            },
        },
        "properties": {
            "layer1_event": {
                "type": "object",
                "required": [
                    "variable_id", "family", "severity", "pathway", "probability",
                ],
                "additionalProperties": False,
                "properties": {
                    "variable_id": {
                        "type": "string",
                        "enum": enum(v["id"] for v in variables),
                        "description": (
                            f"One of the {len(variables)} ledger variables, "
                            "or 'unmapped'."
                        ),
                    },
                    "family": {
                        "type": "string",
                        "enum": enum({v["family"] for v in variables}),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["minor", "moderate", "severe"],
                    },
                    "pathway": {"$ref": "#/$defs/pathway"},
                    "probability": {"$ref": "#/$defs/probability"},
                    "probability_basis": {"type": "string"},
                    "starts_at": {"type": "string", "format": "date-time"},
                    "ends_at": {
                        "oneOf": [
                            {"type": "string", "format": "date-time"},
                            {"type": "null"},
                        ]
                    },
                    "duration_confidence": {
                        "type": "string",
                        "enum": ["stated", "estimated", "unknown"],
                    },
                    "unmapped": {"$ref": "#/$defs/unmapped"},
                },
                "allOf": [{
                    "if": {"properties": {"variable_id": {"const": "unmapped"}}},
                    "then": {"required": ["unmapped"]},
                }],
            },
            "layer2_asset": {
                "type": "object",
                "required": ["asset_id", "mode"],
                "additionalProperties": False,
                "properties": {
                    "asset_id": {"type": "string", "enum": enum(tx["layer2_assets"])},
                    "mode": {
                        "type": "string",
                        "enum": ["road", "rail", "sea", "barge", "multimodal"],
                    },
                    "node_kind": {"type": "string", "enum": enum(tx["layer2_nodes"])},
                    "unmapped": {"$ref": "#/$defs/unmapped"},
                },
                "allOf": [{
                    "if": {"properties": {"asset_id": {"const": "unmapped"}}},
                    "then": {"required": ["unmapped"]},
                }],
            },
            "layer3_channel": {
                "type": "object",
                "required": ["channel_id"],
                "additionalProperties": False,
                "properties": {
                    "channel_id": {
                        "type": "string", "enum": enum(tx["layer3_channels"]),
                    },
                    "free_hours": {
                        "type": "number", "minimum": 0,
                        "description": (
                            "Lateness tolerance. Inside it, nothing is owed."
                        ),
                    },
                    "penalty_shape": {
                        "type": "string",
                        "enum": ["step", "linear", "cliff", "unknown"],
                    },
                    "unmapped": {"$ref": "#/$defs/unmapped"},
                },
                "allOf": [{
                    "if": {"properties": {"channel_id": {"const": "unmapped"}}},
                    "then": {"required": ["unmapped"]},
                }],
            },
            "layer4_cargo": {
                "type": "object",
                "required": ["cargo_class"],
                "additionalProperties": False,
                "properties": {
                    "cargo_class": {
                        "type": "string", "enum": enum(tx["layer4_cargo"]),
                    },
                    "adr_class": {
                        "oneOf": [
                            {"type": "string", "enum": ["3", "8", "9"]},
                            {"type": "null"},
                        ]
                    },
                    "irreversible_if_damaged": {"type": "boolean"},
                    "scrap_fraction_if_damaged": {
                        "type": "number", "minimum": 0, "maximum": 1,
                    },
                    "unmapped": {"$ref": "#/$defs/unmapped"},
                },
                "allOf": [{
                    "if": {"properties": {"cargo_class": {"const": "unmapped"}}},
                    "then": {"required": ["unmapped"]},
                }],
            },
        },
    }


def main() -> int:
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT} is stale — run: python scripts/gen_schema.py")
            return 1
        print(f"{OUT} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
