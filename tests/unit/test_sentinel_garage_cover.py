"""The Sentinel garage rule also watches garage covers (8.26.0).

Behaviour change: garage_left_open used to check only binary sensors with
device class garage_door. A garage door that Home Assistant shows as a cover
with device class garage was never reminded about. It now is, after the same
15 minutes. Nothing else about the rules changes. All state is fake.
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


@pytest.fixture
def sentinel(sentinel_mod, fake_hass):
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                  rules=sentinel_mod.DEFAULT_RULES, entry=None)
    s.calls = []

    async def _fake_announce(entity_id, rule, minutes):
        s.calls.append((entity_id, rule["id"], minutes))
    s._announce_rule = _fake_announce
    return s


def _change(s, eid, state):
    s._handle_state_change(types.SimpleNamespace(data={
        "entity_id": eid, "new_state": types.SimpleNamespace(state=state, attributes={}),
        "old_state": types.SimpleNamespace(state="closed")}))


def _at(monkeypatch, when):
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "utcnow", lambda: when)


def _garage_rule(sentinel_mod):
    return next(r for r in sentinel_mod.DEFAULT_RULES if r["id"] == "garage_left_open")


def test_the_garage_rule_lists_garage_covers(sentinel, fake_hass):
    fake_hass.states.set("cover.big_door", "open", device_class="garage")
    fake_hass.states.set("binary_sensor.roller", "on", device_class="garage_door")
    fake_hass.states.set("cover.blinds", "open", device_class="blind")
    ids = set(sentinel._collect_entity_ids())
    assert {"cover.big_door", "binary_sensor.roller"} <= ids
    assert "cover.blinds" not in ids


async def test_an_open_garage_cover_is_reminded_after_15_minutes(sentinel, fake_hass, monkeypatch):
    fake_hass.states.set("cover.big_door", "open", device_class="garage", friendly_name="Garage")
    _at(monkeypatch, datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
    _change(sentinel, "cover.big_door", "open")
    assert "cover.big_door:garage_left_open" in sentinel._state_start
    sentinel._entity_cache = ["cover.big_door"]
    _at(monkeypatch, datetime(2026, 10, 10, 12, 14, tzinfo=timezone.utc))
    sentinel._check_durations(None)
    await fake_hass.drain()
    assert sentinel.calls == []
    _at(monkeypatch, datetime(2026, 10, 10, 12, 15, tzinfo=timezone.utc))
    sentinel._check_durations(None)
    await fake_hass.drain()
    assert sentinel.calls == [("cover.big_door", "garage_left_open", 15)]


def test_closing_the_cover_clears_it(sentinel, fake_hass):
    fake_hass.states.set("cover.big_door", "open", device_class="garage")
    _change(sentinel, "cover.big_door", "open")
    fake_hass.states.set("cover.big_door", "closed", device_class="garage")
    _change(sentinel, "cover.big_door", "closed")
    assert "cover.big_door:garage_left_open" not in sentinel._state_start


def test_the_garage_door_sensor_still_works(sentinel, fake_hass):
    fake_hass.states.set("binary_sensor.roller", "on", device_class="garage_door")
    _change(sentinel, "binary_sensor.roller", "on")
    assert "binary_sensor.roller:garage_left_open" in sentinel._state_start


@pytest.mark.parametrize("eid,dc,state", [
    ("cover.blinds", "blind", "open"),          # not a garage cover
    ("cover.side_gate", "gate", "open"),
    ("binary_sensor.roller", "garage_door", "open"),   # a sensor says "on", not "open"
    ("cover.big_door", "garage", "on"),         # a cover says "open", not "on"
])
def test_nothing_else_matches(sentinel, fake_hass, eid, dc, state):
    fake_hass.states.set(eid, state, device_class=dc)
    _change(sentinel, eid, state)
    assert f"{eid}:garage_left_open" not in sentinel._state_start


def test_the_other_rules_are_unchanged(sentinel_mod):
    by_id = {r["id"]: r for r in sentinel_mod.DEFAULT_RULES}
    assert [r for r in by_id.values() if r.get("also")] == [by_id["garage_left_open"]]
    rule = _garage_rule(sentinel_mod)
    assert (rule["domain"], rule["device_class"], rule["state"], rule["for_minutes"]) == (
        "binary_sensor", "garage_door", "on", 15)
    assert rule["also"] == [{"domain": "cover", "device_class": "garage", "state": "open"}]
