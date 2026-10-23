"""Excluding an entity silences it at once, even after Nova has learnt it.

Sentinel and the appliance monitor keep their own state about entities they
already watch. Excluding one of them must stop its reports straight away
without waiting for that state to be purged (issue #23): Sentinel's watched
set drops it, a duration clock that is already running stays quiet, and the
announce paths of both check the filter one last time.

The source checks pin where the filter is consulted; the behaviour checks
drive each path with a fake exclusion list.
"""
from __future__ import annotations

import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

NOVA_DIR = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
START = datetime(2026, 2, 3, 9, 0, tzinfo=timezone.utc)


def _body(filename: str, func: str, next_def: str = r"\n\n    (?:async )?def ") -> str:
    """The source of one function, up to the next definition."""
    text = (NOVA_DIR / filename).read_text(encoding="utf-8")
    found = re.search(rf"def {func}\(.*?\n(.*?){next_def}", text, re.S)
    assert found, f"could not isolate {func} in {filename}"
    return found.group(1)


# ── where the filter is consulted ────────────────────────────────────────────

@pytest.mark.parametrize("filename, func, next_def, needles", [
    ("sentinel.py", "_collect_entity_ids", r"\n\n    def ", ("is_excluded",)),
    ("sentinel.py", "_announce_rule", r"\n\n    (?:async )?def ", ("is_excluded", "_excl")),
    ("sentinel.py", "_check_durations", r"\n\n    (?:async )?def ", ("is_excluded", "_excl")),
    ("appliance_monitor.py", "_announce_done", r"\n\n(?:async )?def ", ("is_excluded",)),
])
def test_source_consults_the_exclusion_filter(filename, func, next_def, needles):
    body = _body(filename, func, next_def)
    assert any(n in body for n in needles), f"{func} must consult the exclusion filter"


# ── behaviour ────────────────────────────────────────────────────────────────

@pytest.fixture
def excluded(load, monkeypatch):
    """The set of excluded entity ids that entity_filter.is_excluded reads."""
    chosen: set = set()

    def is_excluded(hass, entity_id, exclusions=None):
        if "*" in chosen:
            raise RuntimeError("filter unavailable")
        return entity_id in chosen

    monkeypatch.setattr(load("entity_filter"), "is_excluded", is_excluded)
    return chosen


@pytest.fixture
def sentinel_mod(load, monkeypatch):
    events = types.ModuleType("homeassistant.helpers.event")
    events.async_track_state_change_event = lambda *a, **k: (lambda: None)
    events.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", events)
    return load("sentinel")


def _sentinel(mod, hass, rules):
    return mod.NovaSentinel(hass, groq_client=None, honorific="sir", rules=rules, entry=None)


def test_watched_set_drops_excluded_entities(sentinel_mod, fake_hass, excluded):
    for eid in ("cover.garage", "cover.shed", "lock.back_door"):
        fake_hass.states.set(eid, "closed")
    rules = [{"id": "back_lock", "entity_id": "lock.back_door"},
             {"id": "covers", "domain": "cover"}]
    s = _sentinel(sentinel_mod, fake_hass, rules)
    assert sorted(s._collect_entity_ids()) == ["cover.garage", "cover.shed", "lock.back_door"]

    excluded.update({"cover.shed", "lock.back_door"})
    assert s._collect_entity_ids() == ["cover.garage"]


async def test_running_clock_goes_quiet_once_excluded(sentinel_mod, fake_hass, excluded,
                                                      monkeypatch):
    rule = {"id": "door_open", "domain": "binary_sensor", "state": "on", "for_minutes": 10}
    s = _sentinel(sentinel_mod, fake_hass, [rule])
    said = []

    async def announce(entity_id, rule, minutes):
        said.append((entity_id, minutes))
    s._announce_rule = announce
    doors = ["binary_sensor.back_door", "binary_sensor.side_door"]
    s._entity_cache = list(doors)
    for eid in doors:
        s._state_start[f"{eid}:door_open"] = START.replace(tzinfo=None)
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "utcnow",
                        lambda: START + timedelta(minutes=12))

    # Both clocks were already running when one door was excluded.
    excluded.add("binary_sensor.back_door")
    s._check_durations(None)
    await fake_hass.drain()
    assert said == [("binary_sensor.side_door", 12)]


@pytest.fixture
def announced(sentinel_mod, load, monkeypatch):
    """What _announce_rule saves and hands to delivery."""
    out = {"saved": [], "delivered": []}

    async def deliver(hass, request):
        out["delivered"].append(request.entity_id)

    monkeypatch.setattr(sentinel_mod, "save_sentinel_event",
                        lambda eid, rule_id, text: out["saved"].append((eid, text)))
    monkeypatch.setattr(sentinel_mod, "save_message", lambda *a, **k: None)
    monkeypatch.setattr(load("delivery"), "async_deliver", deliver)
    return out


def _quiet_sentinel(mod, hass):
    s = _sentinel(mod, hass, [])
    s._household_asleep = lambda: False
    s._live_honorific = lambda: "sir"
    return s


RULE = {"id": "door_open", "message": "{honorific}, {friendly_name} open {minutes} min."}


async def test_announce_rule_skips_an_excluded_entity(sentinel_mod, fake_hass, excluded,
                                                     announced):
    fake_hass.states.set("binary_sensor.back_door", "on", friendly_name="Back Door")
    excluded.add("binary_sensor.back_door")
    await _quiet_sentinel(sentinel_mod, fake_hass)._announce_rule(
        "binary_sensor.back_door", RULE, 15)
    assert announced == {"saved": [], "delivered": []}


@pytest.mark.parametrize("filter_state", ["empty", "broken"])
async def test_announce_rule_speaks_when_not_excluded(sentinel_mod, fake_hass, excluded,
                                                     announced, filter_state):
    # A filter that cannot be read never silences a real alert.
    if filter_state == "broken":
        excluded.add("*")
    fake_hass.states.set("binary_sensor.back_door", "on", friendly_name="Back Door")
    await _quiet_sentinel(sentinel_mod, fake_hass)._announce_rule(
        "binary_sensor.back_door", RULE, 15)
    assert announced["saved"] == [("binary_sensor.back_door", "sir, Back Door open 15 min.")]
    assert announced["delivered"] == ["binary_sensor.back_door"]


@pytest.mark.parametrize("is_excluded, reached", [(True, []), (False, ["sensor.washer_power"])])
async def test_appliance_done_respects_exclusion(load, fake_hass, excluded, monkeypatch,
                                                 is_excluded, reached):
    am = load("appliance_monitor")
    monkeypatch.setattr(am._MON, "hass", fake_hass)
    seen = []

    def never_announce(appliance, label, friendly):
        # The first check after the exclusion gate; stop here either way.
        seen.append("sensor.washer_power")
        return True
    monkeypatch.setattr(am, "_is_never_announce", never_announce)
    if is_excluded:
        excluded.add("sensor.washer_power")
    sensor = am._SensorState(entity_id="sensor.washer_power", friendly_name="Washer",
                             appliance=am.ApplianceType.WASHER,
                             discovery_method="native_status", peak_power=400.0)
    await am._announce_done(sensor, "washer")
    assert seen == reached
