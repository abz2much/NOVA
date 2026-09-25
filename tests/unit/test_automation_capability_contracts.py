"""Characterization of Nova's automation capability contracts (Phase 5).

These pin what callers of the automation capability rely on today: the
public symbols and call signatures of the five root modules, the dictionary
shapes that reach the panel, the agent and the services, the suggestion
store's status vocabulary, and live threshold reads. They were written
against the pre-package code and must keep passing after the code moves
into custom_components/nova/automation/.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
import types

import pytest


def _params(fn) -> list:
    """Parameter names, kinds and defaults (annotation text is ignored)."""
    out = []
    for p in inspect.signature(fn).parameters.values():
        default = None if p.default is inspect.Parameter.empty else repr(p.default)
        out.append((p.name, p.kind.name, default))
    return out


SIGNATURES = {
    "automation_creator": {
        "create_automation": [
            ("hass", "POSITIONAL_OR_KEYWORD", None),
            ("alias", "KEYWORD_ONLY", None),
            ("description", "KEYWORD_ONLY", "''"),
            ("trigger", "KEYWORD_ONLY", "None"),
            ("condition", "KEYWORD_ONLY", "None"),
            ("action", "KEYWORD_ONLY", "None"),
            ("mode", "KEYWORD_ONLY", "'single'"),
            ("request_id", "KEYWORD_ONLY", "None"),
            ("source", "KEYWORD_ONLY", "'ha_service'"),
            ("requested_by_user_id", "KEYWORD_ONLY", "None"),
            ("requested_by_name", "KEYWORD_ONLY", "None"),
            # Phase 5 (D9): additive, so every existing call is unchanged.
            ("request_device_id", "KEYWORD_ONLY", "None"),
        ],
    },
    "automation_inventory": {
        "AutomationContextTracker": [
            ("inventory", "POSITIONAL_OR_KEYWORD", "None"),
            ("ttl", "KEYWORD_ONLY", "21600.0"),
            ("max_entries", "KEYWORD_ONLY", "4096"),
        ],
        "AutomationInventory": [("hass", "POSITIONAL_OR_KEYWORD", None)],
        "get_inventory": [("hass", "POSITIONAL_OR_KEYWORD", None)],
    },
    "automation_matcher": {
        "canonical_config": [("config", "POSITIONAL_OR_KEYWORD", None)],
        "fingerprint": [("config", "POSITIONAL_OR_KEYWORD", None)],
        "action_effects": [("config", "POSITIONAL_OR_KEYWORD", None)],
        "classify": [("candidate", "POSITIONAL_OR_KEYWORD", None),
                     ("records", "POSITIONAL_OR_KEYWORD", None)],
    },
    "automation_trials": {
        "create": [("suggestion_id", "POSITIONAL_OR_KEYWORD", None),
                   ("automation_id", "POSITIONAL_OR_KEYWORD", None),
                   ("installed_at", "POSITIONAL_OR_KEYWORD", "None"),
                   ("db_path", "POSITIONAL_OR_KEYWORD", "None")],
        "record_run": [("automation_entity_id", "POSITIONAL_OR_KEYWORD", None),
                       ("automation_id", "POSITIONAL_OR_KEYWORD", "None"),
                       ("ts", "POSITIONAL_OR_KEYWORD", "None"),
                       ("db_path", "POSITIONAL_OR_KEYWORD", "None")],
        "list_trials": [("db_path", "POSITIONAL_OR_KEYWORD", "None")],
        "set_manual_outcome": [("trial_id", "POSITIONAL_OR_KEYWORD", None),
                               ("verdict", "POSITIONAL_OR_KEYWORD", None),
                               ("ts", "POSITIONAL_OR_KEYWORD", "None"),
                               ("db_path", "POSITIONAL_OR_KEYWORD", "None")],
        "async_handle_triggered": [("hass", "POSITIONAL_OR_KEYWORD", None),
                                   ("event", "POSITIONAL_OR_KEYWORD", None)],
    },
    "pattern_analyzer": {
        "get_analyzer": [],
        "set_thresholds": [("min_occurrences", "POSITIONAL_OR_KEYWORD", "None"),
                           ("confidence", "POSITIONAL_OR_KEYWORD", "None")],
        "normalize_suggestion_automation": [
            ("stored_yaml", "POSITIONAL_OR_KEYWORD", None)],
        "service_for": [("entity_id", "POSITIONAL_OR_KEYWORD", None),
                        ("state", "POSITIONAL_OR_KEYWORD", None)],
        "explain_suggestion": [("pattern_type", "POSITIONAL_OR_KEYWORD", None),
                               ("details", "POSITIONAL_OR_KEYWORD", None),
                               ("count", "POSITIONAL_OR_KEYWORD", None)],
        "_effective_threshold": [],
        "_learned_threshold_delta": [],
    },
}

# Symbols callers, the evaluation harness or tests import from each root
# module. Removing any of them breaks an existing import.
ROOT_SYMBOLS = {
    "automation_creator": {"create_automation", "AutomationWriteError",
                           "_WRITE_LOCK", "_read_automations",
                           "_atomic_write_yaml", "_restore_original"},
    "automation_inventory": {"AutomationRecord", "AutomationInventory",
                             "get_inventory", "SourceAttribution",
                             "AutomationContextTracker",
                             "EVENT_AUTOMATION_RELOADED"},
    "automation_matcher": {"canonical_config", "fingerprint", "action_effects",
                           "classify"},
    "automation_trials": {"create", "record_run", "list_trials",
                          "set_manual_outcome", "async_handle_triggered",
                          "_DEFAULT_DB"},
    "pattern_analyzer": {"DetectedPattern", "PatternAnalyzer", "get_analyzer",
                         "set_thresholds", "normalize_suggestion_automation",
                         "service_for", "explain_suggestion",
                         "install_approved_suggestion", "_effective_threshold",
                         "_learned_threshold_delta", "CONFIDENCE_THRESHOLD",
                         "MIN_OCCURRENCES", "MIN_DAYS", "ANALYSIS_INTERVAL",
                         "DB_PATH", "KNOWLEDGE_FACT_CONFIDENCE"},
}


@pytest.mark.parametrize("module", sorted(ROOT_SYMBOLS))
def test_root_modules_keep_their_symbols(load, module):
    mod = load(module)
    missing = sorted(n for n in ROOT_SYMBOLS[module] if not hasattr(mod, n))
    assert missing == [], f"{module} lost {missing}"


@pytest.mark.parametrize("module", sorted(SIGNATURES))
def test_root_call_signatures_are_unchanged(load, module):
    mod = load(module)
    for name, expected in SIGNATURES[module].items():
        assert _params(getattr(mod, name)) == expected, f"{module}.{name}"


def test_install_approved_suggestion_keeps_its_keywords(load):
    params = {p[0]: p for p in _params(
        load("pattern_analyzer").install_approved_suggestion)}
    assert params["hass"][1] == "POSITIONAL_OR_KEYWORD"
    assert params["suggestion_id"][1] == "POSITIONAL_OR_KEYWORD"
    assert params["requested_by_user_id"] == (
        "requested_by_user_id", "KEYWORD_ONLY", "None")
    assert params["requested_by_name"] == (
        "requested_by_name", "KEYWORD_ONLY", "None")


def test_inventory_public_dict_shape(load):
    inv = load("automation_inventory")
    record = inv.AutomationRecord(entity_id="automation.a", raw_config={"x": 1})
    assert list(record.public_dict()) == [
        "entity_id", "unique_id", "name", "enabled", "last_triggered", "origin",
        "understanding", "blueprint", "referenced_entities",
        "referenced_devices", "referenced_areas"]
    assert "raw_config" not in record.public_dict()


def test_source_attribution_defaults(load):
    src = load("automation_inventory").SourceAttribution()
    assert (src.kind, src.entity_id, src.confidence) == ("unknown", "", 0.0)


def test_classify_result_shape_and_statuses(load):
    matcher = load("automation_matcher")
    config = {"triggers": [{"trigger": "time", "at": "18:00:00"}],
              "actions": [{"action": "light.turn_on", "entity_id": "light.porch"}]}
    record = types.SimpleNamespace(entity_id="automation.porch", name="Porch",
                                   raw_config=config,
                                   referenced_entities=("light.porch",))
    exact = matcher.classify(config, [record])
    assert set(exact) == {"status", "matches", "reason"}
    assert exact["status"] == "already_automated"
    assert exact["matches"] == [{"entity_id": "automation.porch", "name": "Porch"}]
    assert matcher.classify(config, [])["status"] == "new"
    assert matcher.classify(config, [])["matches"] == []


# ── Suggestion store vocabulary ─────────────────────────────────────────────

_SUGGESTIONS_DDL = """
CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    description TEXT NOT NULL,
    automation_yaml TEXT,
    status TEXT DEFAULT 'pending',
    confidence REAL DEFAULT 0.0,
    pattern_count INTEGER DEFAULT 0,
    approved_at TEXT,
    dismissed_at TEXT,
    pattern_type TEXT DEFAULT '',
    entity_ids TEXT DEFAULT '',
    details TEXT DEFAULT '{}'
);
CREATE TABLE state_changes (id INTEGER PRIMARY KEY, timestamp TEXT);
CREATE TABLE commands (id INTEGER PRIMARY KEY, timestamp TEXT);
"""


@pytest.fixture
def side_effects(monkeypatch):
    """Capture decision-record and trial writes instead of touching /config."""
    seen = {"records": [], "outcomes": [], "trials": []}
    dr = types.ModuleType("jc.decision_record")
    dr.record = lambda *a, **k: seen["records"].append((a, k))
    dr.set_outcome_by_ref = lambda *a, **k: seen["outcomes"].append(a)
    dr.outcome_rate = lambda *a, **k: {}
    monkeypatch.setitem(sys.modules, "jc.decision_record", dr)
    return seen


def _analyzer_with_db(load, tmp_path):
    pa = load("pattern_analyzer")
    db = tmp_path / "patterns.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SUGGESTIONS_DDL)
    conn.commit()
    conn.close()
    an = pa.PatternAnalyzer()
    an._db = str(db)
    return pa, an, str(db)


def _pattern(pa, description="light.porch turns on around 18:00 on 9 of 10 days"):
    return pa.DetectedPattern(
        pattern_type="time_routine", description=description,
        entity_ids=["light.porch"], confidence=0.9, occurrences=9,
        details={"hour": 18, "state": "on"})


def _statuses(db):
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT id, status FROM suggestions"))
    finally:
        conn.close()


def test_store_and_status_vocabulary(load, tmp_path, side_effects, monkeypatch):
    pa, an, db = _analyzer_with_db(load, tmp_path)
    trials = []
    monkeypatch.setattr(load("automation_trials"), "create",
                        lambda sid, aid, *a, **k: trials.append((sid, aid)))
    assert an._store_suggestion(_pattern(pa)) is True
    ref = side_effects["records"][0][1]["ref"]
    assert ref == "suggestion:1"

    pending = an.get_pending_suggestions()
    assert [r["id"] for r in pending] == [1]
    assert set(pending[0]) == {
        "id", "created", "description", "automation_yaml", "status",
        "confidence", "pattern_count", "approved_at", "dismissed_at",
        "pattern_type", "entity_ids", "details"}
    assert json.loads(pending[0]["entity_ids"]) == ["light.porch"]
    assert an.get_suggestion(1)["status"] == "pending"

    assert an.approve_suggestion(1) is True
    assert _statuses(db) == {1: "approved"}
    an.mark_covered(1)
    assert _statuses(db) == {1: "already_automated"}
    an.mark_installed(1, "nova_auto_x")
    assert _statuses(db) == {1: "installed"}
    assert ("suggestion:1", "good", "installed") in side_effects["outcomes"]
    assert trials == [(1, "nova_auto_x")]
    assert an.dismiss_suggestion(1) is True
    assert _statuses(db) == {1: "dismissed"}
    assert ("suggestion:1", "unnecessary", "dismiss_suggestion") in side_effects["outcomes"]


def test_get_stats_shape(load, tmp_path, side_effects):
    _pa, an, _db = _analyzer_with_db(load, tmp_path)
    stats = an.get_stats()
    assert {"available", "state_changes", "commands", "pending_suggestions",
            "approved", "dismissed", "days_of_data",
            "ready_for_analysis"} <= set(stats)


# ── Thresholds are process configuration read live by other modules ─────────

def test_threshold_reads_follow_set_thresholds(load, monkeypatch):
    pa = load("pattern_analyzer")
    before = (pa.MIN_OCCURRENCES, pa.CONFIDENCE_THRESHOLD)
    try:
        monkeypatch.setattr(pa, "_learned_threshold_delta", lambda: 0.0)
        pa.set_thresholds(3, 0.42)
        assert pa.MIN_OCCURRENCES == 3
        assert pa.CONFIDENCE_THRESHOLD == 0.42
        assert pa._effective_threshold() == 0.42
        pa.set_thresholds(1, 5.0)            # clamped
        assert pa.MIN_OCCURRENCES == 2
        assert pa.CONFIDENCE_THRESHOLD == 0.95
    finally:
        pa.set_thresholds(*before)


# ── Result shapes other callers read ────────────────────────────────────────

def test_normalize_result_shapes(load):
    pa = load("pattern_analyzer")
    ok = pa.normalize_suggestion_automation(json.dumps({
        "alias": "a", "trigger": {"platform": "time", "at": "18:00:00"},
        "action": {"service": "light.turn_on", "entity_id": "light.p"},
        "condition": {"condition": "sun", "after": "sunset"}}))
    assert set(ok) == {"installable", "alias", "trigger", "action", "condition"}
    advisory = pa.normalize_suggestion_automation(
        json.dumps({"type": "manual_review", "note": "x"}))
    assert set(advisory) == {"installable", "reason"}
    assert advisory["installable"] is False


def test_explain_suggestion_shape(load):
    out = load("pattern_analyzer").explain_suggestion(
        "time_routine", {"hour": 7, "state": "on", "coverage": 0.9}, 12)
    assert set(out) == {"headline", "evidence"}
    assert isinstance(out["evidence"], list)
