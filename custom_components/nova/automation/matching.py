"""Deterministic matching between Nova suggestions and loaded HA automations.

No model call is used here. Exact matches compare a canonical form of trigger,
condition, and action. Configurations Nova cannot safely interpret (notably
blueprints and metadata-only compatibility records) are reported as possible
overlaps instead of being guessed equivalent.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Iterable

from .models import (
    MATCH_EXACT,
    MATCH_NEW,
    MATCH_OPAQUE,
    MATCH_OVERLAP,
    MatchRef,
    MatchResult,
)

_TOP_LEVEL_IGNORED = {
    "id", "alias", "description", "trace", "initial_state", "mode",
}


def _list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _plain(value: Any) -> Any:
    """Stable JSON-safe structure without evaluating templates."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in sorted(
            value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain(item) for item in value), key=repr)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_config(config: Any) -> dict | None:
    """Canonical behavioral subset, or None when no usable config exists."""
    if not isinstance(config, Mapping):
        return None
    if "use_blueprint" in config:
        return None
    triggers = config.get("triggers", config.get("trigger"))
    conditions = config.get("conditions", config.get("condition"))
    actions = config.get("actions", config.get("action"))
    if not triggers or not actions:
        return None

    def modernize(items: Any, kind: str) -> list:
        out = []
        for item in _list(items):
            if isinstance(item, Mapping):
                item = dict(item)
                if kind == "trigger" and "platform" in item and "trigger" not in item:
                    item["trigger"] = item.pop("platform")
                if kind == "action" and "service" in item and "action" not in item:
                    item["action"] = item.pop("service")
            out.append(_plain(item))
        return out

    result = {
        "triggers": modernize(triggers, "trigger"),
        "conditions": modernize(conditions, "condition"),
        "actions": modernize(actions, "action"),
        # Home Assistant's default is single. Normalizing it prevents a
        # missing default from hiding an otherwise exact duplicate while
        # still distinguishing restart/queued/parallel behavior.
        "mode": _plain(config.get("mode", "single")),
    }
    # A few behavior-bearing top-level keys may appear on generated configs in
    # the future. Keep them while discarding identity/presentation metadata.
    for key, value in config.items():
        key = str(key)
        if key in _TOP_LEVEL_IGNORED or key in {
                "trigger", "triggers", "condition", "conditions", "action", "actions"}:
            continue
        result[key] = _plain(value)
    return result


def fingerprint(config: Any) -> str | None:
    canonical = canonical_config(config)
    if canonical is None:
        return None
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _entity_values(value: Any) -> set[str]:
    values = _list(value)
    return {str(item) for item in values
            if isinstance(item, str) and "." in item}


def action_effects(config: Any) -> set[tuple[str, str]]:
    """Return simple ``(service, entity_id)`` effects from an automation."""
    canonical = canonical_config(config)
    if canonical is None:
        return set()
    effects: set[tuple[str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, Mapping):
            return
        service = str(node.get("action") or node.get("service") or "")
        entities = _entity_values(node.get("entity_id"))
        target = node.get("target")
        if isinstance(target, Mapping):
            entities |= _entity_values(target.get("entity_id"))
        data = node.get("data")
        if isinstance(data, Mapping):
            entities |= _entity_values(data.get("entity_id"))
        for entity_id in entities:
            effects.add((service, entity_id))
        # choose/if/repeat/parallel may nest actions. Traversal is bounded by
        # the already-loaded config object and only visits container values.
        for value in node.values():
            if isinstance(value, (Mapping, list, tuple)):
                walk(value)

    walk(canonical["actions"])
    return effects


def classify_result(candidate: Any, records: Iterable[Any]) -> MatchResult:
    """Classify a candidate as new, exact, overlapping, or opaque-related."""
    candidate_fp = fingerprint(candidate)
    candidate_effects = action_effects(candidate)
    candidate_targets = {entity for _service, entity in candidate_effects}
    exact: list[MatchRef] = []
    overlaps: list[MatchRef] = []
    unknown: list[MatchRef] = []

    for record in records:
        entity_id = str(getattr(record, "entity_id", "") or "")
        name = str(getattr(record, "name", "") or entity_id)
        label = MatchRef(entity_id, name)
        raw = getattr(record, "raw_config", None)
        existing_fp = fingerprint(raw)
        if candidate_fp and existing_fp and candidate_fp == existing_fp:
            exact.append(label)
            continue
        effects = action_effects(raw)
        if candidate_effects and effects and candidate_effects.intersection(effects):
            overlaps.append(label)
            continue
        refs = set(getattr(record, "referenced_entities", ()) or ())
        if candidate_targets.intersection(refs):
            (overlaps if existing_fp else unknown).append(label)

    if exact:
        return MatchResult(MATCH_EXACT, tuple(exact),
                           "an equivalent loaded Home Assistant automation exists")
    if overlaps:
        return MatchResult(MATCH_OVERLAP, tuple(overlaps),
                           "a loaded automation controls the same target")
    if unknown:
        return MatchResult(MATCH_OPAQUE, tuple(unknown),
                           "an opaque automation references the same target")
    return MatchResult(MATCH_NEW, (), "no overlap found")


def classify(candidate: Any, records: Iterable[Any]) -> dict[str, Any]:
    """Classify a candidate as new, exact, overlapping, or opaque-related.

    Returns ``{"status", "matches": [{"entity_id", "name"}], "reason"}``."""
    return classify_result(candidate, records).to_dict()
