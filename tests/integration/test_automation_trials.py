"""Automation Trials (Phase 3) — the things tests/unit/'s fakes cannot prove:
a REAL automation_triggered event flowing through hass.bus, real entity
registry resolution, and the listener's lifecycle (registered once at setup,
torn down cleanly on unload — the failed-setup case is covered separately in
test_setup_failure_cleanup.py, which extends the same _left_behind() snapshot
this file's setup uses).
"""
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry


async def _setup_nova(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _isolate_patterns_db(monkeypatch, tmp_path):
    from custom_components.nova import automation_trials
    db = str(tmp_path / "patterns.db")
    monkeypatch.setattr(automation_trials, "_DEFAULT_DB", db)
    return automation_trials, db


def _register_fake_automation_entity(hass, unique_id: str) -> str:
    """Register a real entity-registry entry the way HA's own automation
    component would after `automation.reload` — domain 'automation',
    platform 'automation', keyed by the config's `id:` field as unique_id."""
    reg = er.async_get(hass)
    entry = reg.async_get_or_create(
        "automation", "automation", unique_id,
        suggested_object_id=unique_id.replace("nova_auto_", ""))
    return entry.entity_id


async def test_real_automation_triggered_event_records_run_and_resolves_entity(
    hass, tmp_path, monkeypatch,
):
    at, db = _isolate_patterns_db(monkeypatch, tmp_path)
    trial_id = at.create(1, "nova_auto_porch_light", db_path=db)

    await _setup_nova(hass)
    entity_id = _register_fake_automation_entity(hass, "nova_auto_porch_light")

    hass.bus.async_fire("automation_triggered", {"entity_id": entity_id})
    await hass.async_block_till_done()

    trials = at.list_trials(db_path=db)
    row = next(t for t in trials if t["id"] == trial_id)
    assert row["run_count"] == 1
    assert row["automation_entity_id"] == entity_id
    assert row["last_run"] is not None


async def test_second_real_run_uses_the_fast_resolved_path(hass, tmp_path, monkeypatch):
    at, db = _isolate_patterns_db(monkeypatch, tmp_path)
    at.create(1, "nova_auto_porch_light", db_path=db)

    await _setup_nova(hass)
    entity_id = _register_fake_automation_entity(hass, "nova_auto_porch_light")

    hass.bus.async_fire("automation_triggered", {"entity_id": entity_id})
    await hass.async_block_till_done()
    hass.bus.async_fire("automation_triggered", {"entity_id": entity_id})
    await hass.async_block_till_done()

    row = at.list_trials(db_path=db)[0]
    assert row["run_count"] == 2


async def test_unrelated_automation_event_does_not_affect_trials(hass, tmp_path, monkeypatch):
    """Every automation in the house fires this event, not just Nova's —
    confirmed against a REAL registered (non-Nova) automation entity."""
    at, db = _isolate_patterns_db(monkeypatch, tmp_path)
    at.create(1, "nova_auto_porch_light", db_path=db)

    await _setup_nova(hass)
    other_entity_id = _register_fake_automation_entity(hass, "someone_elses_automation")

    hass.bus.async_fire("automation_triggered", {"entity_id": other_entity_id})
    await hass.async_block_till_done()

    row = at.list_trials(db_path=db)[0]
    assert row["run_count"] == 0
    assert row["automation_entity_id"] is None


async def test_listener_active_after_setup_and_torn_down_on_unload(hass, tmp_path, monkeypatch):
    at, db = _isolate_patterns_db(monkeypatch, tmp_path)
    at.create(1, "nova_auto_porch_light", db_path=db)

    entry = await _setup_nova(hass)
    assert hass.bus.async_listeners().get("automation_triggered", 0) >= 1

    entity_id = _register_fake_automation_entity(hass, "nova_auto_porch_light")
    hass.bus.async_fire("automation_triggered", {"entity_id": entity_id})
    await hass.async_block_till_done()
    assert at.list_trials(db_path=db)[0]["run_count"] == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.bus.async_listeners().get("automation_triggered", 0) == 0

    # firing again after unload must not raise and must not change anything
    hass.bus.async_fire("automation_triggered", {"entity_id": entity_id})
    await hass.async_block_till_done()
    assert at.list_trials(db_path=db)[0]["run_count"] == 1
