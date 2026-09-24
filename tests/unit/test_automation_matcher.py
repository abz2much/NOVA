"""Semantic-enough, deterministic duplicate matching for learned automations."""
from __future__ import annotations

import types


def _module(load):
    return load("automation_matcher")


def _record(entity_id, raw_config, refs=()):
    return types.SimpleNamespace(entity_id=entity_id, name=entity_id,
                                 raw_config=raw_config,
                                 referenced_entities=tuple(refs))


def test_singular_legacy_and_plural_modern_are_exact(load):
    matcher = _module(load)
    candidate = {
        "trigger": {"platform": "time", "at": "20:00:00"},
        "action": {"service": "light.turn_on", "entity_id": "light.porch"},
        "alias": "Nova candidate",
    }
    existing = {
        "id": "abc", "alias": "Porch schedule", "mode": "single",
        "triggers": [{"trigger": "time", "at": "20:00:00"}],
        "conditions": [],
        "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}],
    }
    result = matcher.classify(candidate, [_record("automation.porch", existing)])
    assert result["status"] == "already_automated"
    assert result["matches"][0]["entity_id"] == "automation.porch"


def test_same_effect_different_trigger_is_possible_overlap(load):
    matcher = _module(load)
    candidate = {
        "trigger": {"platform": "time", "at": "20:00:00"},
        "action": {"service": "light.turn_on", "entity_id": "light.porch"},
    }
    existing = {
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on",
                     "target": {"entity_id": "light.porch"}}],
    }
    result = matcher.classify(candidate, [_record("automation.sunset", existing)])
    assert result["status"] == "possible_overlap"


def test_blueprint_reference_is_unknown_not_falsely_new(load):
    matcher = _module(load)
    candidate = {
        "trigger": {"platform": "state", "entity_id": "binary_sensor.motion"},
        "action": {"service": "light.turn_on", "entity_id": "light.porch"},
    }
    blueprint = {"use_blueprint": {"path": "motion_light.yaml", "input": {}}}
    result = matcher.classify(candidate, [
        _record("automation.motion", blueprint, refs=("light.porch",))])
    assert result["status"] == "unknown_overlap"


def test_unrelated_automation_is_new(load):
    matcher = _module(load)
    candidate = {
        "trigger": {"platform": "time", "at": "20:00:00"},
        "action": {"service": "light.turn_on", "entity_id": "light.porch"},
    }
    existing = {
        "triggers": [{"trigger": "time", "at": "08:00:00"}],
        "actions": [{"action": "switch.turn_on", "entity_id": "switch.coffee"}],
    }
    assert matcher.classify(candidate, [_record("automation.coffee", existing)])[
        "status"] == "new"


def test_fingerprint_does_not_contain_sensitive_config(load):
    matcher = _module(load)
    config = {
        "triggers": [{"trigger": "webhook", "webhook_id": "private-value"}],
        "actions": [{"action": "notify.send_message", "data": {"token": "secret"}}],
    }
    value = matcher.fingerprint(config)
    assert len(value) == 64
    assert "private-value" not in value and "secret" not in value


def test_fingerprint_keeps_behavior_bearing_top_level_fields(load):
    matcher = _module(load)
    base = {
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door"}],
        "actions": [{"action": "light.turn_on", "entity_id": "light.hall"}],
        "mode": "single",
        "variables": {"brightness": 20},
    }
    changed = dict(base, mode="restart", variables={"brightness": 80})

    assert matcher.fingerprint(base) != matcher.fingerprint(changed)
