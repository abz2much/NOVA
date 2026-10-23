"""Importing recorder history for Analyze Now without bringing the flood back.

The thinning filter must keep a month of a pulsing motion sensor small, the
store must take imported rows with their own historical times, and the import
must ask the recorder for exactly what live logging would have stored.
"""
from __future__ import annotations

import sqlite3
import sys
import types
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def core(load):
    return load("cognitive_core")


# ── the thinning filter ─────────────────────────────────────────────────────

def _pulsing(count, step=3.0, start=1000.0):
    """A sensor flipping on and off every `step` seconds."""
    return [(start + n * step, "off" if n % 2 == 0 else "on") for n in range(count)]


def test_a_month_of_motion_pulses_is_thinned_to_one_per_interval(core):
    kept = core._backfill_filter_states(_pulsing(20000), interval=300.0, cap=1000)
    # One every 100 pulses survives: well inside the cap.
    assert len(kept) == 200
    assert len(kept) <= 1000
    assert [t for t, _ in kept] == [1000.0 + 300.0 * k for k in range(200)]
    gaps = [later[0] - earlier[0] for earlier, later in zip(kept, kept[1:])]
    assert min(gaps) >= 300.0


def test_the_cap_keeps_the_newest_of_what_the_interval_lets_through(core):
    kept = core._backfill_filter_states(_pulsing(20000), interval=300.0, cap=50)
    assert len(kept) == 50
    assert kept[0][0] == 1000.0 + 300.0 * 150
    assert kept[-1][0] == 1000.0 + 300.0 * 199


def test_unreadable_states_and_repeats_are_dropped(core):
    history = [(100.0, "on"), (101.0, "on"), (500.0, "unavailable"),
               (700.0, "unknown"), (900.0, "off"), (1300.0, "on")]
    kept = core._backfill_filter_states(history, interval=300.0, cap=1000)
    assert kept == [(100.0, "on"), (900.0, "off"), (1300.0, "on")]


def test_occasional_changes_hours_apart_all_survive(core):
    history = [(hour * 3600.0, "on" if hour % 2 == 0 else "off") for hour in range(10)]
    assert core._backfill_filter_states(history, interval=300.0, cap=1000) == history


# ── the store side ──────────────────────────────────────────────────────────

@pytest.fixture
def logger(core, tmp_path):
    """A real StateLogger on its own file (never the real /config)."""
    path = str(tmp_path / "patterns.db")
    return core.StateLogger(db_path=path), path


def test_imported_rows_carry_their_own_time_and_a_history_source(logger):
    store, path = logger
    evening = datetime(2026, 6, 1, 18, 0, 0)
    added = store.bulk_insert_history([
        (evening.isoformat(), "binary_sensor.bay_occupancy", "binary_sensor", "unknown", "on"),
        ((evening + timedelta(minutes=1)).isoformat(), "cover.garage", "cover", "unknown", "closed"),
        ("not a time", "light.hall", "light", "unknown", "on"),
    ])
    assert added == 2                   # the unreadable row is skipped
    assert store.distinct_logged_entities() == {"binary_sensor.bay_occupancy", "cover.garage"}

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT entity_id, new_state, hour, day_of_week, triggered_by, person "
            "FROM state_changes ORDER BY timestamp").fetchall()
    # Hour and weekday come from the row, not from when the import ran.
    assert rows == [
        ("binary_sensor.bay_occupancy", "on", 18, evening.weekday(), "history", "unknown"),
        ("cover.garage", "closed", 18, evening.weekday(), "history", "unknown"),
    ]


def test_an_empty_import_and_an_empty_store(logger):
    store, _path = logger
    assert store.bulk_insert_history([]) == 0
    assert store.distinct_logged_entities() == set()


# ── which entities the import asks the recorder for ─────────────────────────

@pytest.fixture
def asked(core, load, monkeypatch, fake_hass):
    """Run the import against a stand-in recorder and return the ids it
    requested. The panel settings come from `asked.settings`."""
    requested: list = []
    settings: dict = {}
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: settings.get(key, default))

    class _Instance:
        async def async_add_executor_job(self, job):
            return job()

    def significant_states(hass, start, end, entity_ids, **options):
        requested.extend(entity_ids)
        return {}

    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.get_instance = lambda hass: _Instance()
    recorder.history = types.SimpleNamespace(get_significant_states=significant_states)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)
    monkeypatch.setattr(core._CORE, "state_logger", types.SimpleNamespace(
        distinct_logged_entities=lambda: set()))

    async def run():
        requested.clear()
        await core.backfill_from_history(fake_hass)
        return list(requested)
    run.settings = settings
    return run


_HOUSE = ("scene.evening", "event.hall_button", "light.hall", "automation.night",
          "script.bedtime", "input_boolean.guest_mode", "sensor.hall_temperature")


async def test_the_import_follows_the_live_domain_rules(asked, fake_hass):
    """8.49.37: scenes are imported (live logging keeps them for "button then
    scene"); helpers and automations never are; button presses only once
    button learning is on."""
    for entity_id in _HOUSE:
        fake_hass.states.set(entity_id, "on")

    first = await asked()
    assert "scene.evening" in first and "light.hall" in first
    for never in ("event.hall_button", "automation.night", "script.bedtime",
                  "input_boolean.guest_mode", "sensor.hall_temperature"):
        assert never not in first, never

    asked.settings["pattern_learn_buttons"] = True
    assert "event.hall_button" in await asked()


async def test_a_named_noisy_entity_is_imported_once_listed(asked, fake_hass):
    fake_hass.states.set("sensor.hall_temperature", "20")
    assert await asked() == []
    asked.settings["pattern_include_entities"] = ["sensor.hall_temperature"]
    assert await asked() == ["sensor.hall_temperature"]
