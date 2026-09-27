"""Friendly names, not entity ids, wherever a person reads Nova's words.

The analysis runs end to end (PatternAnalyzer.analyze with a fake hass whose
states carry friendly names) into the real suggestion, routine and knowledge
stores, so the wording checked here is what the panel, Home Assistant and the
Memory tab would show.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


def _seed(ps, path):
    conn = sqlite3.connect(path)
    ps.ensure(conn, "pattern_log", "person_patterns")
    base = datetime.now() - timedelta(days=13)

    def ins(eid, state, when, person="unknown"):
        conn.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
            "new_state, area_id, hour, day_of_week, person, triggered_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (when.isoformat(), eid, eid.split(".")[0], "off", state, "",
             when.hour, when.weekday(), person, "device_or_integration"))
    for d in range(12):
        day = base + timedelta(days=d)
        ins("light.kitchen_light", "on", day.replace(hour=7, minute=5), person="abi")
        t = day.replace(hour=20, minute=0, second=0, microsecond=0)
        ins("binary_sensor.front_door", "on", t)
        ins("light.hall_light", "on", t + timedelta(seconds=45))
    conn.commit()
    return conn


@pytest.fixture
def analysis(pa, load, tmp_path, monkeypatch, fake_hass):
    kn = load("knowledge")
    monkeypatch.setattr(kn, "DB_PATH", str(tmp_path / "knowledge.db"))
    monkeypatch.setattr(pa, "_learned_threshold_delta", lambda: 0.0)
    for eid, name in (("person.abi", "Abi"), ("light.kitchen_light", "Kitchen Light"),
                      ("binary_sensor.front_door", "Front Door"),
                      ("light.hall_light", "Hall Light")):
        fake_hass.states.set(eid, "home" if eid.startswith("person") else "off",
                             friendly_name=name)
    db = str(tmp_path / "patterns.db")
    _seed(load("persistence.sqlite"), db).close()
    an = pa.PatternAnalyzer()
    an._db = db

    async def run():
        await an.analyze(fake_hass)
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM suggestions")]
        return rows
    return an, kn, db, run


def _by_type(rows, ptype):
    return [r for r in rows if r["pattern_type"] == ptype]


async def test_suggestion_descriptions_use_names(analysis):
    an, _kn, _db, run = analysis
    rows = await run()
    [routine] = [r for r in _by_type(rows, "time_routine")
                 if json.loads(r["entity_ids"]) == ["light.kitchen_light"]]
    assert routine["description"].startswith("Kitchen Light turns on around 07:00 on ")
    assert routine["description"].endswith(" when Abi is home")
    [seq] = [r for r in _by_type(rows, "sequence")
             if json.loads(r["entity_ids"]) == ["binary_sensor.front_door",
                                                "light.hall_light"]]
    assert seq["description"].startswith(
        "When Front Door turns on, Hall Light turns on shortly after (")
    # ids stay the identity
    assert json.loads(seq["details"])["action"]["entity"] == "light.hall_light"


async def test_automation_names_use_names(analysis):
    _an, _kn, _db, run = analysis
    rows = await run()
    aliases = {json.loads(r["automation_yaml"]).get("alias") for r in rows}
    assert "Nova Learned: Kitchen Light on at 07:00" in aliases
    assert "Nova Learned: Hall Light after Front Door" in aliases


async def test_review_evidence_uses_names(analysis, load):
    _an, _kn, _db, run = analysis
    rows = await run()
    api = load("automation.api")
    items = {tuple(i["entities"]): i for i in api.panel_suggestion_items(rows)}
    seq = items[("binary_sensor.front_door", "light.hall_light")]
    assert "After Front Door → on, Hall Light → on usually follows" in seq["evidence"]
    routine = items[("light.kitchen_light",)]
    assert "Specifically when Abi is home" in routine["evidence"]


async def test_memory_panel_routine_uses_names(analysis):
    an, _kn, _db, run = analysis
    await run()
    [row] = an.get_person_patterns("abi")
    assert row["description"].startswith("Kitchen Light turns on around 07:00")
    assert "light." not in row["description"]


async def test_learned_fact_uses_name_and_moves_the_old_one(analysis):
    """Decision 6: an observed fact learned under the id wording moves to the
    new wording instead of being learned twice; stated facts never move."""
    _an, kn, _db, run = analysis
    kn.remember("kitchen light turns on", "around 07:00 most days", subject="abi",
                source="observed", confidence=0.8)
    kn.remember("hall light turns on", "when I get in", subject="household",
                source="stated")
    await run()
    keys = {(f["subject"], f["key"]) for f in kn.all_facts()}
    assert ("abi", "Kitchen Light turns on") in keys
    assert ("abi", "kitchen light turns on") not in keys
    assert ("household", "hall light turns on") in keys


async def test_old_suggestion_without_identity_is_not_duplicated(analysis):
    """A dismissed suggestion stored before suggestions had details is found
    by its old id wording, so the new wording doesn't bring it back."""
    an, _kn, db, run = analysis
    first = await run()
    [seq] = [r for r in _by_type(first, "sequence")
             if json.loads(r["entity_ids"]) == ["binary_sensor.front_door",
                                                "light.hall_light"]]
    legacy = seq["description"].replace("Front Door", "binary_sensor.front_door") \
                               .replace("Hall Light", "light.hall_light")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM suggestions")
        conn.execute(
            "INSERT INTO suggestions (created, description, status, pattern_type, "
            "entity_ids, details) VALUES ('2026-01-01', ?, 'dismissed', 'sequence', "
            "'', '{}')", (legacy,))
    an._last_analysis = 0
    rows = await run()
    seqs = [r for r in _by_type(rows, "sequence")
            if r["description"] == legacy or "Front Door" in r["description"]]
    assert [(r["status"], r["description"]) for r in seqs] == [("dismissed", legacy)]


async def test_activity_feed_after_unconfirmed_action_uses_name(load, fake_hass, monkeypatch):
    ev = load("entity_verify")
    db = load("database")
    saved = []
    monkeypatch.setattr(db, "save_activity", lambda **kw: saved.append(kw))
    fake_hass.states.set("light.hall_light", "off", friendly_name="Hall Light")
    await ev.record_unverified(fake_hass, "light.hall_light", "turn_on", "agent")
    assert saved[0]["message"].startswith("Hall Light could not be confirmed after turn_on")
    assert saved[0]["entity_id"] == "light.hall_light"


def test_why_did_it_change_uses_names(load, tmp_path, fake_hass):
    rca = load("rca")
    ps = load("persistence.sqlite")
    db = str(tmp_path / "patterns.db")
    conn = sqlite3.connect(db)
    ps.ensure(conn, "pattern_log")
    t = datetime.now().replace(microsecond=0) - timedelta(minutes=5)
    for eid, st, when in (("switch.hall_plug", "unavailable", t),
                          ("light.hall_lamp", "unavailable", t + timedelta(seconds=20))):
        conn.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
            "new_state, area_id, hour, day_of_week) VALUES (?,?,?,?,?,?,?,?)",
            (when.isoformat(), eid, eid.split(".")[0], "on", st, "hall",
             when.hour, when.weekday()))
    conn.commit()
    conn.close()
    fake_hass.states.set("switch.hall_plug", "unavailable", friendly_name="Hall Plug")
    fake_hass.states.set("light.hall_lamp", "unavailable", friendly_name="Hall Lamp")
    out = rca.analyze("light.hall_lamp", patterns_db=db,
                      activity_db=str(tmp_path / "none.db"),
                      names=rca.entity_names(fake_hass))
    assert out["entity_id"] == "light.hall_lamp"
    assert out["candidates"][0]["cause"].startswith("Hall Plug went unavailable")
    assert "Hall Plug went unavailable" in out["summary"]
    assert any(i["text"].startswith("Hall Lamp: ") for i in out["timeline"])
    assert "switch.hall_plug" not in json.dumps(out["candidates"])



def test_condition_phrases_use_names(pa):
    names = {"person.abi": "Abi", "sensor.lounge_temperature": "Lounge Temperature"}
    assert pa._condition_phrase(
        {"condition": "numeric_state", "entity_id": "sensor.lounge_temperature",
         "below": 18.0}, names) == ", mostly while Lounge Temperature is below 18"
    assert pa._condition_phrase(
        {"condition": "state", "entity_id": "person.abi", "state": "home"},
        names) == ", only when Abi is home"
