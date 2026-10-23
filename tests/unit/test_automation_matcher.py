"""Semantic-enough, deterministic duplicate matching for learned automations."""
from __future__ import annotations

import json
import types
import asyncio
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
import pytest
import yaml
from fakes import FakeAutomationInventory as FileInventory, FakeHass


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


def test_installed_presence_release_is_exact(load):
    matcher = _module(load)
    suggestions = load("automation.suggestions")
    models = load("automation.models")
    pattern = models.DetectedPattern(
        "sequence", "door then light",
        ["binary_sensor.door", "light.office"], 0.9, 8,
        details={
            "trigger": {"entity": "binary_sensor.door", "state": "on"},
            "action": {"entity": "light.office", "state": "on"},
            "condition": [{
                "condition": "state",
                "entity_id": "binary_sensor.office_presence",
                "state": "on",
            }],
            "presence_gate": {
                "entity_id": "binary_sensor.office_presence",
                "area_id": "office", "area_name": "Office",
            },
            "presence_release": {
                "entity_id": "binary_sensor.office_presence",
                "area_id": "office", "area_name": "Office",
                "settle_seconds": 60,
            },
        },
    )
    generated = suggestions.generate_automation(pattern)
    installed = suggestions.normalize_suggestion_automation(generated)
    candidate = {
        "triggers": installed["trigger"],
        "conditions": installed["condition"],
        "actions": installed["action"],
        "mode": installed["mode"],
    }
    existing = {**json.loads(generated), "id": "nova_release"}

    result = matcher.classify(candidate, [_record("automation.office", existing)])
    assert result["status"] == "already_automated"


# ── matching with templates, blueprints and names ─────────────────────────────

def test_match_payload_carries_names_only_in_matches(load):
    matcher = load("automation.matching")
    existing = _record_p5("automation.sunset", {
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]})
    out = matcher.classify({"triggers": [{"trigger": "time", "at": "20:00:00"}],
                            "actions": [{"action": "light.turn_on",
                                         "entity_id": "light.porch"}]}, [existing])
    assert set(out) == {"status", "matches", "reason"}
    assert out["matches"] == [{"entity_id": "automation.sunset",
                               "name": "automation.sunset"}]


def _record_p5(entity_id, raw_config, refs=()):
    return types.SimpleNamespace(entity_id=entity_id, name=entity_id,
                                 raw_config=raw_config, referenced_entities=tuple(refs))


_PORCH = {"triggers": [{"trigger": "time", "at": "20:00:00"}],
          "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]}


def test_templated_target_is_never_proof_of_new(load):
    matcher = load("automation.matching")
    templated = _record_p5("automation.dynamic", {
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on",
                     "target": {"entity_id": "{{ states.light | map(attribute='entity_id') | list }}"}}]})
    out = matcher.classify(_PORCH, [templated])
    assert out["status"] == "unknown_overlap"
    assert out["matches"][0]["entity_id"] == "automation.dynamic"


def test_template_strings_are_not_entity_ids(load):
    matcher = load("automation.matching")
    effects = matcher.action_effects({
        "triggers": [{"trigger": "sun"}],
        "actions": [{"action": "light.turn_on",
                     "entity_id": "{{ 'light.porch' if is_state('sun.sun', 'below_horizon') else 'light.hall' }}"}]})
    assert effects == set()


def test_opaque_blueprint_with_no_references_is_uncertain(load):
    matcher = load("automation.matching")
    blueprint = _record_p5("automation.bp", {"use_blueprint": {"path": "x.yaml", "input": {}}})
    assert matcher.classify(_PORCH, [blueprint])["status"] == "unknown_overlap"
    metadata_only = _record_p5("automation.meta", None)
    assert matcher.classify(_PORCH, [metadata_only])["status"] == "unknown_overlap"


def test_templated_candidate_is_never_new_or_a_duplicate(load):
    matcher = load("automation.matching")
    candidate = {"triggers": [{"trigger": "template",
                               "value_template": "{{ is_state('sun.sun', 'below_horizon') }}"}],
                 "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]}
    similar = _record_p5("automation.other", {
        "triggers": [{"trigger": "template",
                      "value_template": "{{ states('sun.sun') == 'below_horizon' }}"}],
        "actions": [{"action": "light.turn_off", "entity_id": "light.hall"}]})
    assert matcher.classify(candidate, [])["status"] == "unknown_overlap"
    assert matcher.classify(candidate, [similar])["status"] == "unknown_overlap"


def test_unrelated_plain_automation_is_still_new(load):
    matcher = load("automation.matching")
    other = _record_p5("automation.hall", {
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "light.turn_on", "entity_id": "light.hall"}]},
        refs=("light.hall",))
    assert matcher.classify(_PORCH, [other])["status"] == "new"


def test_blueprint_with_unrelated_references_stays_uncertain(load):
    matcher = load("automation.matching")
    blueprint = _record_p5("automation.bp_hall",
                        {"use_blueprint": {"path": "motion_light.yaml",
                                           "input": {"light": "light.hall"}}},
                        refs=("light.hall", "binary_sensor.hall_motion"))
    out = matcher.classify(_PORCH, [blueprint])
    assert out["status"] == "unknown_overlap"
    assert out["matches"] == [{"entity_id": "automation.bp_hall",
                               "name": "automation.bp_hall"}]


def test_blueprint_with_only_device_or_area_references_stays_uncertain(load):
    matcher = load("automation.matching")
    blueprint = types.SimpleNamespace(
        entity_id="automation.bp_area", name="Area lights", referenced_entities=(),
        referenced_devices=("device-porch-light",), referenced_areas=("porch",),
        raw_config={"use_blueprint": {"path": "area_lights.yaml",
                                      "input": {"area": "porch"}}})
    assert matcher.classify(_PORCH, [blueprint])["status"] == "unknown_overlap"


def test_metadata_only_record_with_unrelated_references_stays_uncertain(load):
    matcher = load("automation.matching")
    meta = _record_p5("automation.meta_hall", None, refs=("light.hall",))
    assert matcher.classify(_PORCH, [meta])["status"] == "unknown_overlap"


def test_templated_record_with_literal_targets_stays_uncertain(load):
    matcher = load("automation.matching")
    templated = _record_p5("automation.hall_if", {
        "triggers": [{"trigger": "template", "value_template": "{{ is_state('sun.sun', 'below_horizon') }}"}],
        "actions": [{"action": "light.turn_on", "entity_id": "light.hall"}]},
        refs=("light.hall",))
    assert matcher.classify(_PORCH, [templated])["status"] == "unknown_overlap"


def test_identical_template_text_is_an_exact_duplicate(load):
    matcher = load("automation.matching")
    config = {"triggers": [{"trigger": "template",
                            "value_template": "{{ is_state('sun.sun', 'below_horizon') }}"}],
              "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]}
    existing = _record_p5("automation.porch_dark", {
        "id": "x", "alias": "Porch after dark", "mode": "single", **config})
    out = matcher.classify(config, [existing])
    assert out["status"] == "already_automated"
    assert out["matches"][0]["entity_id"] == "automation.porch_dark"
