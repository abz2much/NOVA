"""Tests for the door-gated arrival briefing (v7.101.9). Real bug this fixes:
the welcome briefing used to fire the instant a person's GPS/zone flipped to
"home" -- often while they were still in the driveway or car, before they'd
actually walked in. Now it waits for the configured front door to actually
open. No front door configured means arrival briefings stay off entirely
(a deliberate choice -- no silent fallback to the old premature behavior)."""
from pathlib import Path

import pytest


class _FakeState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class _FakeEvent:
    def __init__(self, entity_id, new_state, old_state=None):
        self.data = {
            "entity_id": entity_id,
            "new_state": new_state,
            "old_state": old_state,
        }


@pytest.fixture
def pb(load, tmp_path, monkeypatch, fake_hass):
    jcfg = load("nova_config")
    monkeypatch.setattr(jcfg, "CONFIG_PATH", Path(tmp_path / "config.json"))
    monkeypatch.setattr(jcfg, "_cache", {})
    monkeypatch.setattr(jcfg, "_loaded", False)

    mod = load("proactive_briefing")
    mod._STATE.hass = fake_hass
    mod._STATE.running = True
    mod._STATE.pending_arrival_person = ""
    mod._STATE.pending_arrival_ts = 0.0
    mod._STATE.last_arrival_briefing = 0.0

    briefed = []

    async def _fake_trigger(reason, person_name=""):
        briefed.append((reason, person_name))
    monkeypatch.setattr(mod, "_trigger_briefing", _fake_trigger)
    mod._briefed = briefed  # stash for assertions
    return mod


def _set_front_door(pb, entity_id):
    import sys
    jcfg = sys.modules["jc.nova_config"]
    jcfg.set("arrival_front_door_entity", entity_id)


def test_no_front_door_configured_person_arrival_stays_silent(pb, fake_hass):
    pb._on_state_changed(_FakeEvent(
        "person.alex", _FakeState("home", {"friendly_name": "Alex"}),
        _FakeState("not_home"),
    ))
    assert pb._STATE.pending_arrival_person == ""
    assert pb._briefed == []


def test_person_arrival_with_door_configured_does_not_brief_immediately(pb, fake_hass):
    _set_front_door(pb, "binary_sensor.front_door")
    pb._on_state_changed(_FakeEvent(
        "person.alex", _FakeState("home", {"friendly_name": "Alex"}),
        _FakeState("not_home"),
    ))
    assert pb._STATE.pending_arrival_person == "Alex"
    assert pb._briefed == []  # not yet -- waiting for the door


async def test_door_opening_after_pending_arrival_triggers_briefing(pb, fake_hass):
    _set_front_door(pb, "binary_sensor.front_door")
    pb._on_state_changed(_FakeEvent(
        "person.alex", _FakeState("home", {"friendly_name": "Alex"}),
        _FakeState("not_home"),
    ))
    pb._on_state_changed(_FakeEvent(
        "binary_sensor.front_door", _FakeState("on"), _FakeState("off"),
    ))
    await fake_hass.drain()
    assert pb._briefed == [("arrival", "Alex")]
    assert pb._STATE.pending_arrival_person == ""


async def test_unrelated_door_opening_does_not_consume_pending_arrival(pb, fake_hass):
    _set_front_door(pb, "binary_sensor.front_door")
    pb._on_state_changed(_FakeEvent(
        "person.alex", _FakeState("home", {"friendly_name": "Alex"}),
        _FakeState("not_home"),
    ))
    pb._on_state_changed(_FakeEvent(
        "binary_sensor.back_door", _FakeState("on"), _FakeState("off"),
    ))
    await fake_hass.drain()
    assert pb._briefed == []
    assert pb._STATE.pending_arrival_person == "Alex"  # still pending


async def test_stale_pending_arrival_does_not_brief(pb, fake_hass, monkeypatch):
    _set_front_door(pb, "binary_sensor.front_door")
    pb._on_state_changed(_FakeEvent(
        "person.alex", _FakeState("home", {"friendly_name": "Alex"}),
        _FakeState("not_home"),
    ))
    # Simulate 11 minutes passing (> ARRIVAL_DOOR_WINDOW_S) before the door opens.
    pb._STATE.pending_arrival_ts -= (pb.ARRIVAL_DOOR_WINDOW_S + 60)
    pb._on_state_changed(_FakeEvent(
        "binary_sensor.front_door", _FakeState("on"), _FakeState("off"),
    ))
    await fake_hass.drain()
    assert pb._briefed == []
    assert pb._STATE.pending_arrival_person == ""  # cleared, not left dangling


async def test_second_door_open_without_new_arrival_does_not_rebrief(pb, fake_hass):
    _set_front_door(pb, "binary_sensor.front_door")
    pb._on_state_changed(_FakeEvent(
        "person.alex", _FakeState("home", {"friendly_name": "Alex"}),
        _FakeState("not_home"),
    ))
    pb._on_state_changed(_FakeEvent(
        "binary_sensor.front_door", _FakeState("on"), _FakeState("off"),
    ))
    await fake_hass.drain()
    assert len(pb._briefed) == 1
    # Door closes and opens again -- no new pending arrival, must not re-brief.
    pb._on_state_changed(_FakeEvent(
        "binary_sensor.front_door", _FakeState("off"), _FakeState("on"),
    ))
    pb._on_state_changed(_FakeEvent(
        "binary_sensor.front_door", _FakeState("on"), _FakeState("off"),
    ))
    await fake_hass.drain()
    assert len(pb._briefed) == 1


async def test_changing_front_door_entity_is_read_live_not_cached(pb, fake_hass):
    # arrival_front_door_entity is read fresh via nova_config each call, not
    # frozen at start() -- so changing it in Settings takes effect without a
    # reload.
    _set_front_door(pb, "binary_sensor.old_door")
    pb._on_state_changed(_FakeEvent(
        "person.alex", _FakeState("home", {"friendly_name": "Alex"}),
        _FakeState("not_home"),
    ))
    _set_front_door(pb, "binary_sensor.new_door")
    pb._on_state_changed(_FakeEvent(
        "binary_sensor.old_door", _FakeState("on"), _FakeState("off"),
    ))
    await fake_hass.drain()
    assert pb._briefed == []  # old door no longer the configured one

    pb._on_state_changed(_FakeEvent(
        "binary_sensor.new_door", _FakeState("on"), _FakeState("off"),
    ))
    await fake_hass.drain()
    assert pb._briefed == [("arrival", "Alex")]
