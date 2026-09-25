"""Load and validate evaluation scenarios from fixtures/*.json.

Each fixture file holds a list of scenarios. To add one, append an object
with every REQUIRED key below to the fixture for its category (or a new
fixture file); `load_scenarios` rejects anything malformed with the file
and scenario ID named. Expectations set to null are "not applicable" and
are left out of the matching metric's denominator.
"""
from __future__ import annotations

import json
import pathlib

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
BASELINE = FIXTURES / "baseline.json"
METRICS_BASELINE = FIXTURES / "metrics_baseline.json"

REQUIRED = ("id", "description", "category", "runner", "state", "input",
            "safety", "expected", "provider")
EXPECTED_REQUIRED = ("decision", "tool", "entities", "clarification_required",
                     "announcement_allowed", "confirmation_required")


class ScenarioError(ValueError):
    """A fixture is malformed. This is a harness error, never a Nova result."""


def validate(sc: dict, source: str = "") -> None:
    from .harness import RUNNERS, SAFETY_CHECKS
    where = f"{source}:{sc.get('id', '?')}"
    missing = [k for k in REQUIRED if k not in sc]
    if missing:
        raise ScenarioError(f"{where} is missing {missing}")
    if sc["runner"] not in RUNNERS:
        raise ScenarioError(f"{where} names unknown runner {sc['runner']!r}")
    unknown = [p for p in sc["safety"] if p not in SAFETY_CHECKS]
    if unknown:
        raise ScenarioError(f"{where} names unknown safety properties {unknown}")
    missing = [k for k in EXPECTED_REQUIRED if k not in sc["expected"]]
    if missing:
        raise ScenarioError(f"{where} expected block is missing {missing}")
    extra = set(sc["expected"]) - set(EXPECTED_REQUIRED) - {"extra"}
    if extra:
        raise ScenarioError(f"{where} expected block has unknown keys {sorted(extra)}")


def load_scenarios(ids=None, categories=None, directory: pathlib.Path = FIXTURES) -> list:
    """All scenarios, sorted by ID, optionally filtered by ID or category."""
    seen: dict = {}
    for path in sorted(directory.glob("*.json")):
        if path.name in (BASELINE.name, METRICS_BASELINE.name):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ScenarioError(f"{path.name} must hold a list of scenarios")
        for sc in data:
            validate(sc, path.name)
            if sc["id"] in seen:
                raise ScenarioError(f"duplicate scenario id {sc['id']} in {path.name}")
            seen[sc["id"]] = sc
    out = [seen[k] for k in sorted(seen)]
    if ids:
        unknown = sorted(set(ids) - set(seen))
        if unknown:
            raise ScenarioError(f"unknown scenario ids {unknown}")
        out = [s for s in out if s["id"] in set(ids)]
    if categories:
        out = [s for s in out if s["category"] in set(categories)]
    return out


def load_metrics_baseline(path: pathlib.Path = METRICS_BASELINE) -> dict:
    """Pinned numerator, denominator and failing IDs for every metric."""
    return json.loads(path.read_text(encoding="utf-8"))


def load_baseline(path: pathlib.Path = BASELINE) -> dict:
    """Pinned verdicts for scenarios that do not pass today. A scenario not
    listed here is expected to pass."""
    return json.loads(path.read_text(encoding="utf-8"))
