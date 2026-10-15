"""History backfill for Analyze Now — safety-critical pieces.

The anti-flood core (_backfill_filter_states) must keep a motion-storm history
bounded, and the store methods must round-trip. This is what lets backfill import
past behavior without reintroducing the flood or double-counting.
"""
import sqlite3
import sys
import types
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def cc(load):
    return load("cognitive_core")


def test_motion_storm_history_is_bounded(cc):
    # 30 days of a motion sensor pulsing every 3s, imported through the filter
    events = [(1000.0 + i * 3.0, "on" if i % 2 else "off") for i in range(20000)]
    kept = cc._backfill_filter_states(events, interval=300.0, cap=1000)
    assert len(kept) <= 1000, f"backfill not capped: {len(kept)}"
    # spacing respects the rate limit
    for a, b in zip(kept, kept[1:]):
        assert b[0] - a[0] >= 300.0


def test_filter_drops_unavailable_and_nochange(cc):
    events = [
        (100.0, "on"), (101.0, "on"),          # no-change
        (500.0, "unavailable"),                 # dropped
        (900.0, "off"), (1300.0, "on"),
    ]
    kept = cc._backfill_filter_states(events, interval=300.0, cap=1000)
    states = [s for _, s in kept]
    assert "unavailable" not in states
    assert states == ["on", "off", "on"]       # first on, then off, then on


def test_sparse_events_all_kept(cc):
    # occupancy-style: a few events hours apart — none dropped by the rate limit
    events = [(i * 3600.0, "on" if i % 2 == 0 else "off") for i in range(10)]
    kept = cc._backfill_filter_states(events, interval=300.0, cap=1000)
    assert len(kept) == 10


def test_bulk_insert_and_distinct(cc, tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    # db_path injected → StateLogger builds its real schema here, never /config
    sl = cc.StateLogger(db_path=db)
    t = datetime(2026, 6, 1, 18, 0, 0)
    n = sl.bulk_insert_history([
        (t.isoformat(), "binary_sensor.bay_occupancy", "binary_sensor", "unknown", "on"),
        ((t + timedelta(minutes=1)).isoformat(), "cover.garage", "cover", "unknown", "closed"),
    ])
    assert n == 2
    got = sl.distinct_logged_entities()
    assert got == {"binary_sensor.bay_occupancy", "cover.garage"}
    # hour/day_of_week derive from the historical timestamp, not now
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT hour, triggered_by FROM state_changes "
                       "WHERE entity_id='binary_sensor.bay_occupancy'").fetchone()
    conn.close()
    assert row[0] == 18 and row[1] == "history"


async def test_backfill_follows_live_logging_domain_rules(load, fake_hass, monkeypatch):
    """8.49.37: Analyze Now imports scenes (live logging keeps them for
    "button then scene") and imports button presses only when button
    learning is on."""
    core = load("cognitive_core")
    jc = load("nova_config")
    store = {}
    monkeypatch.setattr(jc, "get", lambda k, d=None: store.get(k, d))
    for eid in ("scene.evening", "event.hall_button", "light.hall", "automation.night"):
        fake_hass.states.set(eid, "on")
    asked = []

    class _Recorder:
        async def async_add_executor_job(self, func):
            return func()

    def _states(hass, start, end, ids, **kw):
        asked.extend(ids)
        return {}

    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.get_instance = lambda hass: _Recorder()
    recorder.history = types.SimpleNamespace(get_significant_states=_states)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)

    class _Logger:
        def distinct_logged_entities(self):
            return set()

    monkeypatch.setattr(core._CORE, "state_logger", _Logger())
    await core.backfill_from_history(fake_hass)
    assert "scene.evening" in asked and "light.hall" in asked
    assert "event.hall_button" not in asked and "automation.night" not in asked

    store["pattern_learn_buttons"] = True
    asked.clear()
    await core.backfill_from_history(fake_hass)
    assert "event.hall_button" in asked
