"""Tests for NovaSentinel's duration-based rules (door/window/lock left open).

Regression guard for a live-caught bug: `_check_durations` used to reset
`_state_start` (the original open time) on every re-announce, so a door left
open kept reporting the same "10 minutes" forever instead of escalating to
20, 30, 40 as it stayed open. Also covers the new `skip_if_outdoor_above_c`
gate (mild weather -> no left-open noise).
"""
import sys
import types
from datetime import datetime, timezone

import pytest


@pytest.fixture
def sentinel_mod(load, monkeypatch):
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    return load("sentinel")


def _door_rule(**overrides):
    rule = {
        "id": "door_left_open",
        "domain": "binary_sensor",
        "device_class": "door",
        "state": "on",
        "for_minutes": 10,
        "message": "{honorific}, {friendly_name} has been open for {minutes} minutes.",
    }
    rule.update(overrides)
    return rule


def _make_sentinel(sentinel_mod, fake_hass, rules):
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=rules, entry=None)
    calls = []

    async def _fake_announce(entity_id, rule, minutes):
        calls.append((entity_id, rule["id"], minutes))
    s._announce_rule = _fake_announce
    s.calls = calls
    return s


def _at(sentinel_mod, monkeypatch, when):
    dt_mod = sys.modules["homeassistant.util.dt"]
    monkeypatch.setattr(dt_mod, "utcnow", lambda: when)


async def test_reannounce_escalates_instead_of_repeating(sentinel_mod, fake_hass, monkeypatch):
    rule = _door_rule()
    s = _make_sentinel(sentinel_mod, fake_hass, [rule])
    opened = datetime(2026, 9, 14, 12, 0)
    s._state_start["binary_sensor.front_door:door_left_open"] = opened

    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 10, tzinfo=timezone.utc))
    s._entity_cache = ["binary_sensor.front_door"]
    s._check_durations(None)
    await fake_hass.drain()

    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 20, tzinfo=timezone.utc))
    s._check_durations(None)
    await fake_hass.drain()

    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc))
    s._check_durations(None)
    await fake_hass.drain()

    assert s.calls == [
        ("binary_sensor.front_door", "door_left_open", 10),
        ("binary_sensor.front_door", "door_left_open", 20),
        ("binary_sensor.front_door", "door_left_open", 30),
    ]


async def test_no_reannounce_before_next_threshold(sentinel_mod, fake_hass, monkeypatch):
    rule = _door_rule()
    s = _make_sentinel(sentinel_mod, fake_hass, [rule])
    opened = datetime(2026, 9, 14, 12, 0)
    s._state_start["binary_sensor.front_door:door_left_open"] = opened
    s._entity_cache = ["binary_sensor.front_door"]

    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 10, tzinfo=timezone.utc))
    s._check_durations(None)
    await fake_hass.drain()

    # a minute later -- well short of the next 10-minute mark
    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 11, tzinfo=timezone.utc))
    s._check_durations(None)
    await fake_hass.drain()

    assert s.calls == [("binary_sensor.front_door", "door_left_open", 10)]


async def test_state_resolving_clears_reannounce_tracking(sentinel_mod, fake_hass):
    rule = _door_rule()
    s = _make_sentinel(sentinel_mod, fake_hass, [rule])
    key = "binary_sensor.front_door:door_left_open"
    s._state_start[key] = datetime(2026, 9, 14, 12, 0)
    s._last_announced[key] = datetime(2026, 9, 14, 12, 10)

    s._entity_matches_rule = lambda eid, r: True
    event = types.SimpleNamespace(data={
        "entity_id": "binary_sensor.front_door",
        "new_state": types.SimpleNamespace(state="off"),
    })
    s._handle_state_change(event)

    assert key not in s._state_start
    assert key not in s._last_announced


async def test_temperature_gate_skips_when_mild(sentinel_mod, fake_hass, monkeypatch):
    rule = _door_rule(skip_if_outdoor_above_c=10)
    s = _make_sentinel(sentinel_mod, fake_hass, [rule])
    s._state_start["binary_sensor.front_door:door_left_open"] = \
        datetime(2026, 9, 14, 12, 0)
    s._entity_cache = ["binary_sensor.front_door"]
    s._outdoor_temp_c = lambda: 15.0  # mild -- above the 10C gate

    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 10, tzinfo=timezone.utc))
    s._check_durations(None)
    await fake_hass.drain()

    assert s.calls == []


async def test_temperature_gate_fires_when_cold(sentinel_mod, fake_hass, monkeypatch):
    rule = _door_rule(skip_if_outdoor_above_c=10)
    s = _make_sentinel(sentinel_mod, fake_hass, [rule])
    s._state_start["binary_sensor.front_door:door_left_open"] = \
        datetime(2026, 9, 14, 12, 0)
    s._entity_cache = ["binary_sensor.front_door"]
    s._outdoor_temp_c = lambda: 4.0  # cold -- below the gate, should still nag

    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 10, tzinfo=timezone.utc))
    s._check_durations(None)
    await fake_hass.drain()

    assert s.calls == [("binary_sensor.front_door", "door_left_open", 10)]


async def test_temperature_gate_unavailable_falls_back_to_announcing(
        sentinel_mod, fake_hass, monkeypatch):
    """No outdoor temp source configured -- don't let the gate silently
    swallow every left-open alert; fail open (announce) instead."""
    rule = _door_rule(skip_if_outdoor_above_c=10)
    s = _make_sentinel(sentinel_mod, fake_hass, [rule])
    s._state_start["binary_sensor.front_door:door_left_open"] = \
        datetime(2026, 9, 14, 12, 0)
    s._entity_cache = ["binary_sensor.front_door"]
    s._outdoor_temp_c = lambda: None

    _at(sentinel_mod, monkeypatch, datetime(2026, 9, 14, 12, 10, tzinfo=timezone.utc))
    s._check_durations(None)
    await fake_hass.drain()

    assert s.calls == [("binary_sensor.front_door", "door_left_open", 10)]
