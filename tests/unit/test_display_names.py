"""Friendly names on every surface that shows text written with entity_ids.

v7.125.0 named entities in new wording. Rows stored by older releases, log
lines, decision records, the analysis diagnostic, Service Health details and
the alias of an older pending suggestion still carried raw entity_ids. These
tests feed those real stored shapes (the exact 7.124.x wording) through the
display paths and check a person sees names, while ids, keys and automation
payloads stay exactly as stored.

websocket.py needs a real websocket_api to import, so its small helpers are
extracted with ast (as test_websocket_decisions.py does) and run inside the
synthetic `jc` package so their relative imports resolve.
"""
from __future__ import annotations

import ast
import json
import types

import pytest
from ws_sources import ws_top_level


NAMES = {
    "device_tracker.home_cloud": "Home Cloud",
    "light.kitchen": "Kitchen Light",
    "binary_sensor.front_door": "Front Door",
    "light.hall": "Hall Light",
}

# The routine alert and log line from 7.124.x, verbatim in shape.
OLD_ALERT = ("Around this time you usually device_tracker.home_cloud turns "
             "not_home around 11:00 on 16 of 24 days when Abi is home.")


def _ws_helpers(load, *names):
    load("cognitive.naming")
    mod = types.ModuleType("jc._ws_display_stub")
    mod.__dict__["__package__"] = "jc"
    exec("from typing import Any, Optional\nHomeAssistant = object\n", mod.__dict__)
    for _path, src, node in ws_top_level():
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_DECISION_TEXT_FIELDS"
                for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<ws>", "exec"),
                 mod.__dict__)
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.get_source_segment(src, node), "<ws_fn>", "exec"),
                 mod.__dict__)
    return mod


# ── the pure naming helpers ─────────────────────────────────────────────

def test_humanize_text_names_known_ids_only(load):
    naming = load("cognitive.naming")
    assert naming.humanize_text(OLD_ALERT, NAMES) == (
        "Around this time you usually Home Cloud turns not_home around 11:00 "
        "on 16 of 24 days when Abi is home.")
    # Unknown ids, ordinary dotted text and numbers are left alone.
    text = "sensor.unknown_thing, e.g. 1.5 kWh, see file.txt"
    assert naming.humanize_text(text, NAMES) == text
    assert naming.humanize_text("anything", {}) == "anything"
    assert naming.humanize_text(None, NAMES) is None


def test_humanize_text_keeps_an_id_already_beside_its_name(load):
    naming = load("cognitive.naming")
    text = "Kitchen Light (light.kitchen) in kitchen changed from off to on"
    assert naming.humanize_text(text, NAMES) == text


def test_humanize_record_labels_ids_and_leaves_the_original(load):
    naming = load("cognitive.naming")
    rec = {"entity_id": "light.kitchen", "note": "light.kitchen went on",
           "list": ["binary_sensor.front_door", 3], "n": 2}
    out = naming.humanize_record(rec, NAMES)
    assert out == {"entity_id": "Kitchen Light (light.kitchen)",
                   "note": "Kitchen Light went on",
                   "list": ["Front Door (binary_sensor.front_door)", 3], "n": 2}
    assert rec["entity_id"] == "light.kitchen"


# ── pending suggestions stored by an older release ──────────────────────

def _old_sequence_row():
    yaml = json.dumps({
        "alias": "Nova Learned: light.hall after binary_sensor.front_door",
        "trigger": {"platform": "state", "entity_id": "binary_sensor.front_door",
                    "to": "on"},
        "action": [{"service": "light.turn_on", "entity_id": "light.hall"}],
    }, indent=2)
    return {
        "id": 7, "created": "2026-09-20T20:00:00", "pattern_type": "sequence",
        "description": ("When binary_sensor.front_door turns on, light.hall turns "
                        "on shortly after (9 times in 30 days, ~45s later)"),
        "automation_yaml": yaml, "confidence": 0.8, "pattern_count": 9,
        "entity_ids": json.dumps(["binary_sensor.front_door", "light.hall"]),
        "details": json.dumps({
            "trigger": {"entity": "binary_sensor.front_door", "state": "on"},
            "action": {"entity": "light.hall", "state": "on"}}),
    }


def test_old_pending_suggestion_is_shown_with_names(load):
    api = load("automation.api")
    item = api.panel_suggestion_items([_old_sequence_row()], NAMES)[0]
    assert item["description"].startswith(
        "When Front Door turns on, Hall Light turns on shortly after")
    assert "binary_sensor." not in " ".join(item["evidence"])
    payload = json.loads(item["yaml"])
    assert payload["alias"] == "Nova Learned: Hall Light after Front Door"
    # The automation itself keeps its entity_ids.
    assert payload["trigger"]["entity_id"] == "binary_sensor.front_door"
    assert payload["action"][0]["entity_id"] == "light.hall"


def test_without_names_suggestions_are_unchanged(load):
    api = load("automation.api")
    row = _old_sequence_row()
    item = api.panel_suggestion_items([row])[0]
    assert item["description"] == row["description"]
    assert item["yaml"] == row["automation_yaml"]


def test_installed_alias_is_named(load, fake_hass):
    inst = load("automation.installation")
    for eid, name in NAMES.items():
        fake_hass.states.set(eid, "off", friendly_name=name)
    alias = inst._named_alias(
        fake_hass, "Nova Learned: light.hall after binary_sensor.front_door",
        _old_sequence_row()["details"])
    assert alias == "Nova Learned: Hall Light after Front Door"


def test_installed_alias_prefers_names_learned_with_the_suggestion(load, fake_hass):
    inst = load("automation.installation")
    details = json.dumps({"names": {"light.hall": "Hall Light"}})
    assert inst._named_alias(fake_hass, "Nova Learned: light.hall on", details) \
        == "Nova Learned: Hall Light on"
    # Nothing known: unchanged.
    assert inst._named_alias(fake_hass, "Nova Learned: light.x on", None) \
        == "Nova Learned: light.x on"


# ── websocket display helpers ───────────────────────────────────────────

def test_log_lines_are_named_for_the_panel_only(load):
    ws = _ws_helpers(load, "_named_log_entries")
    entries = [{"ts": "11:23:41", "cat": "LEARN", "msg": "anticipation: " + OLD_ALERT}]
    shown = ws._named_log_entries(entries, NAMES)
    assert "device_tracker." not in shown[0]["msg"]
    assert "Home Cloud turns not_home" in shown[0]["msg"]
    assert entries[0]["msg"].startswith("anticipation: Around this time you usually "
                                        "device_tracker.home_cloud")


def test_decision_text_fields_are_named_ids_kept(load):
    ws = _ws_helpers(load, "_named_decision")
    rec = {"id": 4, "kind": "anticipation_routine", "outcome": None,
           "observation": {"person": "abi", "entity_id": "light.kitchen"},
           "reason": "light.kitchen usually turns on now"}
    out = ws._named_decision(rec, NAMES)
    assert out["observation"]["entity_id"] == "Kitchen Light (light.kitchen)"
    assert out["reason"] == "Kitchen Light usually turns on now"
    assert out["id"] == 4 and out["kind"] == "anticipation_routine"
    assert rec["reason"].startswith("light.kitchen")


def test_analysis_diagnostic_carries_names(load):
    ws = _ws_helpers(load, "_name_diagnostic")
    res = {"diagnostic": {"candidates": [{"entity_id": "light.kitchen"}],
                          "top_sources": [{"entity_id": "sensor.nameless_x"}]}}
    ws._name_diagnostic(res, NAMES)
    assert res["diagnostic"]["candidates"][0]["name"] == "Kitchen Light"
    assert res["diagnostic"]["top_sources"][0]["name"] == "nameless x"


def test_memory_routines_are_named(load, monkeypatch):
    ws = _ws_helpers(load, "_get_person_routines")
    pa = load("automation.patterns")

    class _A:
        def get_person_patterns(self):
            return [{"id": 1, "person": "abi", "pattern_type": "time_routine",
                     "description": ("light.kitchen turns on around 07:00 on 11 "
                                     "of 21 days when abi is home"),
                     "confidence": 0.8, "occurrences": 11, "last_seen": ""}]
    monkeypatch.setattr(pa, "get_analyzer", lambda: _A())
    out = ws._get_person_routines(NAMES)
    assert out["abi"][0]["description"].startswith("Kitchen Light turns on")


# ── Service Health ──────────────────────────────────────────────────────

def test_service_health_detail_names_the_entity(load, fake_hass):
    sh = load("diagnostics.service_health")
    fake_hass.states.set("tts.piper", "idle", friendly_name="Piper")
    out = sh._check_speech_entity(fake_hass, "tts", "TTS", "tts.piper")
    assert out["detail"] == "Piper (tts.piper) available"
    assert out["entity"] == "tts.piper"
