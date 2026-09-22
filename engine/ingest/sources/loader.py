"""sources.yaml -> SourceSpec. The file Sika edits instead of writing Python.

THE PROMISE THIS FILE MAKES
===========================
Adding a feed to this radar is a block of YAML. Not a subclass, not a plugin
entry point, not a pull request against the engine. That matters more than it
sounds: the integration work at a company like Sika is done by people who have
a TMS endpoint and a two-week window, and any design that makes them open a
Python file will simply not happen.

So the contract is: name the URL, name where the items are, name which field
means what. Everything else — timeouts, retries, fixture fallback, the honest
status on the /inputs panel, the funnel, the challenger — they get for free.

WHAT THE FILE MAY NOT CONTAIN
-----------------------------
A credential. Auth is declared as a *variable name*, and the value is read from
the environment at fetch time. ``.gitignore`` protects a file somebody might
still email; a value that was never in the file cannot be emailed at all.

REFUSING LOUDLY, HERE AND NOWHERE ELSE
--------------------------------------
This loader raises on a malformed spec, unlike the mapper, which shrugs at a
malformed *item*. The difference is who made the mistake. A bad item is the
upstream's doing and must not take the board down; a bad spec is ours, it is in
our config, and a typo that silently disables a feed is how a planner ends up
trusting a board with a hole in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engine.ingest.sources.catalog import builtin_specs
from engine.ingest.sources.spec import Auth, Cost, FieldMap, Nature, SourceSpec

_FIELD_NAMES = set(FieldMap.__dataclass_fields__)


class SourceConfigError(ValueError):
    """A spec in sources.yaml is wrong. Names the key and the problem."""


def load(path: Path | str | None) -> list[SourceSpec]:
    """Built-ins, with sources.yaml overriding them and adding custom ones.

    A missing file is not an error — it means "the built-in catalogue, as
    shipped", which is a complete and runnable configuration.
    """
    specs = {s.key: s for s in builtin_specs()}
    if path is None:
        return list(specs.values())

    file = Path(path)
    if not file.exists():
        return list(specs.values())

    try:
        document = yaml.safe_load(file.read_text()) or {}
    except yaml.YAMLError as exc:
        raise SourceConfigError(f"{file} is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise SourceConfigError(f"{file}: expected a mapping at the top level")

    for key, patch in (document.get("builtin") or {}).items():
        if key not in specs:
            raise SourceConfigError(
                f"builtin.{key} is not a built-in source. Known: "
                f"{', '.join(sorted(specs))}"
            )
        specs[key] = _patch(specs[key], patch, where=f"builtin.{key}")

    for entry in document.get("custom") or []:
        spec = _custom(entry)
        if spec.key in specs:
            raise SourceConfigError(
                f"custom source {spec.key!r} collides with a built-in of the "
                "same name — pick another key, or patch it under `builtin:`"
            )
        specs[spec.key] = spec

    return list(specs.values())


# ---------------------------------------------------------------------
def _patch(spec: SourceSpec, patch: Any, where: str) -> SourceSpec:
    """Only the fields a deployment has any business changing."""
    if patch is None:
        return spec
    if not isinstance(patch, dict):
        raise SourceConfigError(f"{where}: expected a mapping, got {type(patch).__name__}")

    allowed = {"enabled", "params", "headers", "source_tier", "fixture", "url"}
    unknown = set(patch) - allowed
    if unknown:
        raise SourceConfigError(
            f"{where}: cannot override {', '.join(sorted(unknown))}. "
            f"Overridable: {', '.join(sorted(allowed))}. To change how a "
            "built-in is parsed, copy it into `custom:` under a new key."
        )

    merged: dict[str, Any] = {}
    if "params" in patch:
        merged["params"] = {**spec.params, **(patch["params"] or {})}
    if "headers" in patch:
        merged["headers"] = {**spec.headers, **(patch["headers"] or {})}
    for simple in ("enabled", "source_tier", "fixture", "url"):
        if simple in patch:
            merged[simple] = patch[simple]

    return _replace(spec, merged)


def _custom(entry: Any) -> SourceSpec:
    if not isinstance(entry, dict):
        raise SourceConfigError(f"custom: expected a mapping, got {type(entry).__name__}")

    key = entry.get("key")
    if not key:
        raise SourceConfigError("custom: every source needs a `key`")
    where = f"custom.{key}"

    missing = [f for f in ("label", "url", "fields") if not entry.get(f)]
    if missing:
        raise SourceConfigError(f"{where}: missing required field(s): {', '.join(missing)}")

    raw_fields = entry["fields"]
    if not isinstance(raw_fields, dict):
        raise SourceConfigError(f"{where}.fields: expected a mapping")
    if not raw_fields.get("headline"):
        raise SourceConfigError(
            f"{where}.fields: `headline` is required — an item with no text is "
            "not a report of anything, and the funnel would spend a model call "
            "on an empty string"
        )
    unknown = set(raw_fields) - _FIELD_NAMES
    if unknown:
        raise SourceConfigError(
            f"{where}.fields: unknown field(s) {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(sorted(_FIELD_NAMES))}"
        )

    try:
        nature = Nature(entry.get("nature", "report"))
    except ValueError as exc:
        raise SourceConfigError(
            f"{where}.nature: must be 'report' (needs reading) or 'instrument' "
            "(a measured number). Instruments never reach a model."
        ) from exc

    try:
        cost = Cost(entry.get("cost", "free"))
    except ValueError as exc:
        raise SourceConfigError(
            f"{where}.cost: must be one of {', '.join(c.value for c in Cost)}"
        ) from exc

    auth_raw = entry.get("auth") or {}
    if not isinstance(auth_raw, dict):
        raise SourceConfigError(f"{where}.auth: expected a mapping")
    for banned in ("token", "secret", "key_value", "password", "value"):
        if banned in auth_raw:
            raise SourceConfigError(
                f"{where}.auth.{banned}: a credential must never appear in this "
                "file. Use `env: NAME_OF_VARIABLE` and set it in .env instead."
            )

    try:
        return SourceSpec(
            key=str(key),
            label=str(entry["label"]),
            nature=nature,
            cost=cost,
            source_tier=int(entry.get("source_tier", 2)),
            url=str(entry["url"]),
            items_path=str(entry.get("items_path", "")),
            fields=FieldMap(**{k: str(v) for k, v in raw_fields.items()}),
            date_format=str(entry.get("date_format", "iso")),
            params={str(k): str(v) for k, v in (entry.get("params") or {}).items()},
            headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
            auth=Auth(
                kind=str(auth_raw.get("kind", "none")),
                env=str(auth_raw.get("env", "")),
                name=str(auth_raw.get("name", "")),
            ),
            fixture=str(entry.get("fixture", "")),
            families=tuple(entry.get("families") or ()),
            enabled=bool(entry.get("enabled", True)),
            unlocks_if_connected=str(entry.get("unlocks_if_connected", "")),
            notes=str(entry.get("notes", "")),
            builtin=False,
        )
    except (ValueError, TypeError) as exc:
        raise SourceConfigError(f"{where}: {exc}") from exc


def _replace(spec: SourceSpec, changes: dict[str, Any]) -> SourceSpec:
    current = {f: getattr(spec, f) for f in SourceSpec.__dataclass_fields__}
    current.update(changes)
    return SourceSpec(**current)
