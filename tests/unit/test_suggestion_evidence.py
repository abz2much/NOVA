"""The evidence shown with an automation suggestion.

A suggestion is only reviewable if the person can see why Nova made it.
explain_suggestion turns a pattern's stored details into a headline and a list
of observations; the suggestions table keeps the pattern type, entities and
details beside each row so the panel can rebuild that explanation later; the
panel renders it next to the approve and dismiss buttons.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest
from ws_sources import ws_text
import yaml

NOVA_DIR = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


def _source(*parts: str) -> str:
    return NOVA_DIR.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture
def explain(load):
    return load("pattern_analyzer").explain_suggestion


# ── explaining each kind of pattern ──────────────────────────────────────────

WHY_CASES = {
    "routine_from_counts": (
        "time_routine",
        {"hour": 18, "state": "on", "consistency": 0.87, "person": "Alex"}, 26,
        "A daily routine around 18:00",
        ["Observed turning on near 18:00", "Happened 26 times in the last 30 days",
         "Consistent on about 87% of days", "Specifically when Alex is home"]),
    "routine_with_misses_and_owner_name": (
        "time_routine",
        {"hour": 7, "minute": 30, "state": "off", "observed_days": 20,
         "opportunity_days": 24, "coverage": 0.5, "person": "alex",
         "condition": {"condition": "state", "entity_id": "person.alex", "state": "home"},
         "names": {"person.alex": "Alex"}}, 20,
        "A daily routine around 07:30",
        ["Observed turning off near 07:30", "Happened on 20 of 24 days (missed 4)",
         "Consistent on about 50% of days", "Specifically when Alex is home"]),
    "routine_never_missed": (
        "time_routine", {"hour": 6, "observed_days": 9, "opportunity_days": 9}, 9,
        "A daily routine around 06:00", ["Happened on 9 of 9 days"]),
    "routine_without_hour": (
        "time_routine", {}, 4,
        "A daily routine around a regular time", ["Happened 4 times in the last 30 days"]),
    "command": (
        "repeated_command", {"command": "lights off", "hour": 23}, 14,
        "A command you give often",
        ["You've asked 'lights off' 14 times", "Most often around 23:00"]),
    "sequence_in_words": (
        "sequence", {"first": "door opens", "then": "hall light on", "window_seconds": 30}, 9,
        "One action reliably follows another",
        ["After door opens, hall light on usually follows", "Seen 9 times in 30 days",
         "Usually within 30s"]),
    "unknown_kind": (
        "something_new", {}, 5, "A learned pattern", ["Observed 5 times in 30 days"]),
    "temperature_without_target": (
        "temp_pref", {}, 3, "A temperature preference", ["Observed 3 times"]),
    "temperature_with_target": (
        "temp_pref", {"target": 21}, 6, "A temperature preference",
        ["Set to 21° repeatedly", "Observed 6 times"]),
    "presence_empty": (
        "presence", {}, 4, "A presence-linked pattern", ["Correlated 4 times over 30 days"]),
}


@pytest.mark.parametrize("kind, details, count, headline, evidence",
                         list(WHY_CASES.values()), ids=list(WHY_CASES))
def test_explanation(explain, kind, details, count, headline, evidence):
    assert explain(kind, details, count) == {"headline": headline, "evidence": evidence}


def test_sequence_steps_are_named_and_presence_is_explained(explain):
    out = explain("sequence", {
        "trigger": {"entity": "binary_sensor.front_door", "state": "on"},
        "action": {"entity": "light.office", "state": "on"},
        "presence_gate": {"area_name": "Office"},
        "presence_release": {"area_name": "Office", "settle_seconds": 60},
    }, 8)
    text = " ".join(out["evidence"])
    # Steps read as names, never as raw dicts or entity ids.
    assert "{'entity'" not in text and "binary_sensor" not in text
    assert out["evidence"] == [
        "After front door → on, office → on usually follows",
        "Seen 8 times in 30 days",
        "Only when presence is detected in Office",
        "Turns off again once presence in Office has cleared for 1 minute",
    ]


@pytest.mark.parametrize("settle, spoken", [
    (60, "1 minute"), (120, "2 minutes"), (90, "90 seconds"), (0, "0 seconds"),
    (-30, "0 seconds"),
])
def test_release_settle_time_is_spoken_naturally(explain, settle, spoken):
    out = explain("sequence", {"presence_release": {"area_name": "Hall",
                                                    "settle_seconds": settle}}, 2)
    assert out["evidence"][-1] == (
        f"Turns off again once presence in Hall has cleared for {spoken}")


@pytest.mark.parametrize("details", [None, {}, {"hour": "xx"}, {"consistency": None},
                                     {"names": "not a dict", "hour": 9}])
def test_junk_details_never_raise(explain, details):
    out = explain("time_routine", details, 0)
    assert set(out) == {"headline", "evidence"}
    assert out["headline"] and out["evidence"]


def test_unreadable_hour_falls_back_to_a_plain_line(explain):
    assert explain("time_routine", {"hour": "xx"}, 3) == {
        "headline": "A learned pattern", "evidence": ["Observed 3 times"]}


# ── the evidence is stored with the suggestion and comes back out ────────────

@pytest.fixture
def store(load, tmp_path, monkeypatch):
    """A SuggestionStore over a patterns.db built from Nova's own schema."""
    path = tmp_path / "patterns.db"
    conn = sqlite3.connect(str(path))
    for statement in load("persistence.schema").COMPONENTS["pattern_log"].statements:
        conn.execute(statement)
    conn.commit()
    conn.close()
    # The proposal is also written to the decision log; keep that off disk.
    monkeypatch.setattr(load("decision_record"), "record", lambda *a, **k: None)
    return load("automation.suggestions").SuggestionStore(str(path))


def test_stored_row_keeps_evidence_for_the_panel(load, store):
    details = {"hour": 18, "state": "on", "observed_days": 12, "opportunity_days": 14}
    pattern = load("automation.models").DetectedPattern(
        pattern_type="time_routine", description="porch light at six",
        entity_ids=["light.porch"], confidence=0.8, occurrences=12, details=details)
    assert store.store(pattern) is True

    (row,) = store.pending()
    assert row["pattern_type"] == "time_routine"
    assert json.loads(row["entity_ids"]) == ["light.porch"]
    assert json.loads(row["details"]) == details

    (item,) = load("automation.api").panel_suggestion_items([row])
    assert item["why_headline"] == "A daily routine around 18:00"
    assert item["evidence"] == ["Observed turning on near 18:00",
                                "Happened on 12 of 14 days (missed 2)"]


# ── the wiring, read from source ─────────────────────────────────────────────

def test_an_old_suggestions_table_gains_the_evidence_columns(load, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute("CREATE TABLE suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "created TEXT NOT NULL, description TEXT NOT NULL, automation_yaml TEXT, "
                 "status TEXT DEFAULT 'pending', confidence REAL DEFAULT 0.0, "
                 "pattern_count INTEGER DEFAULT 0, approved_at TEXT, dismissed_at TEXT)")
    load("persistence.sqlite").ensure(conn, "pattern_log")
    columns = {row[1]: row[4] for row in conn.execute("PRAGMA table_info(suggestions)")}
    conn.close()
    assert columns["pattern_type"] == "''" and columns["entity_ids"] == "''"
    assert columns["details"] == "'{}'"


def test_panel_data_carries_the_explanation():
    assert "panel_suggestion_items" in ws_text()


@pytest.mark.parametrize("needle", [
    "why_headline", "What Nova observed", "s.evidence", "<li>", "Create automation",
    # The action handler binds to these classes on the richer card.
    "sug-approve", "sug-dismiss", "sug-yaml-btn",
])
def test_panel_renders_evidence_and_keeps_actions(needle):
    assert needle in _source("frontend", "nova-panel.js")


# ── the panel's suggestion card ───────────────────────────────────────────────

def test_panel_keeps_a_failed_suggestion_actionable():
    import contract_extract as ce
    js = (ce.COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    wire = js[js.index("_wireSuggestions() {"):js.index("// ─── Settings")]
    assert "if (res && res.ok)" in wire
    assert "buttons.forEach(b => b.disabled = false)" in wire
    assert "res.reason" in wire


def test_panel_reads_the_matched_automation():
    import contract_extract as ce
    js = (ce.COMP / "frontend" / "nova-panel.js").read_text(encoding="utf-8")
    block = js[js.index("const match = s.automation_match"):js.index("return `", js.index("const match = s.automation_match"))]
    assert "(match.matches || [])[0]" in block
    assert "match.name" not in block and "match.entity_id" not in block
