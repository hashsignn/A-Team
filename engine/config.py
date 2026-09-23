"""Configuration loading with the config/ -> config.example/ fallback.

BRIEF §8.3: ``engine/`` knows nothing about Sika. Everything customer-specific
lives in ``config/`` (gitignored) and falls back to ``config.example/`` (public,
synthetic, committed). "Onboarding a new customer is a profile swap" is then a
true statement rather than a claim.

Anything loaded from ``config.example/`` is flagged, so the /inputs panel can
say plainly which parts of the board are running on the synthetic stand-in.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from engine.schemas import DelayTriple, Mode, Node, RiskVariable

ROOT = Path(__file__).resolve().parent.parent
CUSTOMER_DIR = ROOT / "config"
EXAMPLE_DIR = ROOT / "config.example"


@dataclass
class LoadedFile:
    name: str
    path: Path
    is_example: bool
    data: Any


@dataclass
class Config:
    """The whole configuration surface, loaded once per run."""

    files: dict[str, LoadedFile] = field(default_factory=dict)

    # ---------------------------------------------------------------
    def raw(self, name: str) -> Any:
        return self.files[name].data

    def is_example(self, name: str) -> bool:
        return self.files[name].is_example

    @property
    def version(self) -> str:
        """Hash of every config file, stamped onto results.

        Two runs that disagree should be explainable by either the as-of or the
        config, so both are recorded.
        """
        h = hashlib.sha256()
        for name in sorted(self.files):
            h.update(name.encode())
            h.update(self.files[name].path.read_bytes())
        return h.hexdigest()[:12]

    # ---------------------------------------------------------------
    # Typed accessors
    # ---------------------------------------------------------------
    @property
    def nodes(self) -> dict[str, Node]:
        if not hasattr(self, "_nodes"):
            nodes = {}
            for entry in self.raw("network")["nodes"]:
                node = Node(**entry)
                nodes[node.id] = node
            self._nodes = nodes
        return self._nodes

    @property
    def lanes(self) -> list[dict]:
        return self.raw("lanes")["lanes"]

    @property
    def variables(self) -> dict[str, RiskVariable]:
        if not hasattr(self, "_variables"):
            variables = {}
            for entry in self.raw("variables")["variables"]:
                var = RiskVariable(**entry)
                variables[var.id] = var
            self._variables = variables
        return self._variables

    @property
    def scoring(self) -> dict:
        return self.raw("scoring")

    @property
    def thresholds(self) -> dict:
        return self.raw("thresholds")

    @property
    def contacts(self) -> dict:
        return self.raw("contacts")

    # ---------------------------------------------------------------
    def delay_triple(self, variable_id: str, severity: str) -> DelayTriple:
        """The three-point estimate for a variable at a severity.

        Overrides first, then the family default. Held at family level so the
        parameter count stays at the ~90 that BRIEF §5.0 claims — a claim that
        only survives if it is actually true.
        """
        model = self.raw("delay_model")
        overrides = model.get("overrides") or {}
        if variable_id in overrides and severity in overrides[variable_id]:
            return DelayTriple(**overrides[variable_id][severity])

        var = self.variables.get(variable_id)
        if var is None:
            raise KeyError(f"unknown variable {variable_id!r}")
        family_defaults = model["defaults"].get(var.family)
        if family_defaults is None:
            raise KeyError(
                f"no delay model for family {var.family!r} "
                f"(variable {variable_id!r})"
            )
        return DelayTriple(**family_defaults[severity])

    def min_action_hours(self, action_type: str) -> float | None:
        """Hours needed to execute an action, or None if not configured.

        None means "we have not stated this", and the caller must render it as
        unknown. It must never become a zero, which would silently make an
        action look available forever.
        """
        table = self.scoring.get("min_action_hours", {})
        return table.get(action_type)

    def variables_for_family(self, family: str) -> list[RiskVariable]:
        return [v for v in self.variables.values() if v.family == family]

    @property
    def families(self) -> list[str]:
        seen: list[str] = []
        for var in self.variables.values():
            if var.family not in seen:
                seen.append(var.family)
        return seen


_FILES = (
    "company_profile",
    "taxonomy",
    "network",
    "lanes",
    "variables",
    "delay_model",
    "scoring",
    "thresholds",
    "contacts",
    "sources",
    "fast",
)

# sources.yaml is optional: with no file, the built-in free catalogue is
# the configuration, and that is complete and runnable on its own.
_OPTIONAL = {"company_profile", "sources", "fast"}


def load_config(
    customer_dir: Path | None = None,
    example_dir: Path | None = None,
) -> Config:
    customer_dir = customer_dir or CUSTOMER_DIR
    example_dir = example_dir or EXAMPLE_DIR

    cfg = Config()
    for name in _FILES:
        filename = f"{name}.yaml"
        customer_path = customer_dir / filename
        example_path = example_dir / filename

        if customer_path.exists():
            path, is_example = customer_path, False
        elif example_path.exists():
            path, is_example = example_path, True
        elif name in _OPTIONAL:
            continue
        else:
            raise FileNotFoundError(
                f"required config {filename!r} not found in {customer_dir} "
                f"or {example_dir}"
            )

        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        cfg.files[name] = LoadedFile(
            name=name, path=path, is_example=is_example, data=data
        )

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    """Fail loudly at load time rather than subtly at scoring time."""
    nodes = cfg.nodes
    problems: list[str] = []

    for node in nodes.values():
        for alt in node.alternatives:
            if alt not in nodes:
                problems.append(f"node {node.id}: unknown alternative {alt!r}")

    for lane in cfg.lanes:
        previous_to: str | None = None
        for i, leg in enumerate(lane["legs"]):
            for key in ("from", "to"):
                if leg[key] not in nodes:
                    problems.append(
                        f"lane {lane['id']} leg {i}: unknown node {leg[key]!r}"
                    )
            if previous_to is not None and leg["from"] != previous_to:
                problems.append(
                    f"lane {lane['id']} leg {i}: starts at {leg['from']!r} but "
                    f"the previous leg ended at {previous_to!r} — legs must be "
                    "an ordered chain"
                )
            previous_to = leg["to"]
            try:
                Mode(leg["mode"])
            except ValueError:
                problems.append(f"lane {lane['id']} leg {i}: bad mode {leg['mode']!r}")

    # Every variable must resolve to a delay triple at every severity, or the
    # first time that combination occurs will be at scoring time on stage.
    for var_id in cfg.variables:
        for severity in ("minor", "moderate", "severe"):
            try:
                cfg.delay_triple(var_id, severity)
            except Exception as exc:  # noqa: BLE001 - surfaced below
                problems.append(f"variable {var_id} / {severity}: {exc}")

    if problems:
        raise ValueError(
            "configuration is invalid:\n  " + "\n  ".join(problems)
        )
