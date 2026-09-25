"""Phase 5 automation capability against a real Home Assistant: a real
executor thread pool (D1), and a real automation component reloading a real
automations.yaml so installation is confirmed by Home Assistant itself
(D2, D4, D6)."""
import sqlite3
from datetime import datetime, timedelta

import yaml
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry


async def _setup(hass, automations: list):
    config_dir = hass.config.config_dir
    with open(hass.config.path("configuration.yaml"), "w", encoding="utf-8") as fh:
        fh.write("automation: !include automations.yaml\n")
    with open(hass.config.path("automations.yaml"), "w", encoding="utf-8") as fh:
        fh.write(yaml.safe_dump(automations) if automations else "[]\n")
    assert config_dir
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "automation", {"automation": automations})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _seed_history(db: str, days: int = 12) -> None:
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS state_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        entity_id TEXT NOT NULL, domain TEXT NOT NULL, old_state TEXT,
        new_state TEXT NOT NULL, area_id TEXT, hour INTEGER, day_of_week INTEGER,
        triggered_by TEXT DEFAULT 'system', person TEXT DEFAULT 'unknown');
    CREATE TABLE IF NOT EXISTS commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, text TEXT NOT NULL,
        handled_by TEXT DEFAULT 'agent', entity_ids TEXT DEFAULT '[]',
        person TEXT DEFAULT 'unknown', hour INTEGER, day_of_week INTEGER);
    """)
    base = datetime.now() - timedelta(days=days + 2)
    for d in range(days):
        when = (base + timedelta(days=d)).replace(hour=18, minute=0, second=0)
        conn.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
            "new_state, area_id, hour, day_of_week) VALUES (?,?,?,?,?,?,?,?)",
            (when.isoformat(), "light.porch", "light", "off", "on", "", 18,
             when.weekday()))
    conn.commit()
    conn.close()


async def test_pattern_analysis_finds_routines_on_the_real_executor(
        hass, tmp_path, monkeypatch):
    from custom_components.nova.automation import patterns
    db = str(tmp_path / "patterns.db")
    _seed_history(db)
    analyzer = patterns.PatternAnalyzer()
    analyzer._db = db
    stored = []
    monkeypatch.setattr(analyzer, "_store_suggestion",
                        lambda p: (stored.append(p), True)[1])
    monkeypatch.setattr(analyzer, "_store_person_pattern", lambda p: False)
    monkeypatch.setattr(analyzer, "_promote_to_knowledge", lambda pats: 0)
    monkeypatch.setattr(patterns, "_learned_threshold_delta", lambda: 0.0)

    found = await analyzer.analyze(hass)

    assert any(p.pattern_type == "time_routine" for p in found)
    assert stored


async def test_installation_is_confirmed_by_home_assistant(hass, monkeypatch):
    from custom_components.nova import action_log
    from custom_components.nova.automation import installation
    monkeypatch.setattr(action_log, "start", lambda *a, **k: 1)
    monkeypatch.setattr(action_log, "set_execution", lambda *a, **k: None)
    legacy = [{"id": "legacy", "alias": "Legacy",
               "triggers": [{"trigger": "sun", "event": "sunset"}],
               "actions": [{"action": "light.turn_on", "entity_id": "light.hall"}]}]
    await _setup(hass, legacy)
    path = hass.config.path("automations.yaml")

    result = await installation.create_automation(
        hass, alias="Porch evening",
        trigger={"trigger": "time", "at": "18:00:00"},
        action={"action": "light.turn_on", "entity_id": "light.porch"})
    await hass.async_block_till_done()

    assert result["success"] is True, result
    with open(path, encoding="utf-8") as fh:
        ids = [item["id"] for item in yaml.safe_load(fh)]
    assert ids == ["legacy", result["automation_id"]]
    registry_ids = {state.attributes.get("id")
                    for state in hass.states.async_all("automation")}
    assert result["automation_id"] in registry_ids

    with open(path, "rb") as fh:
        before = fh.read()
    again = await installation.create_automation(
        hass, alias="Porch evening",
        trigger={"trigger": "time", "at": "18:00:00"},
        action={"action": "light.turn_on", "entity_id": "light.porch"})
    assert again["success"] is False
    assert "already exists" in again["error"]
    with open(path, "rb") as fh:
        assert fh.read() == before
