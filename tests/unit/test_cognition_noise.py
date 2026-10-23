"""Keeping cognition's escalations quiet (cognition.process).

A numeric reading is never escalated on statistics alone (8.40.0: 100 of
104 rated alerts were unnecessary, many of them voltage or server load
spikes). A purely statistical escalation is allowed once per entity per
cooldown. Safety, access and device problems are never held back.

The module keeps its learned model in module level tables, so every test
starts and ends with a reset, and runs on a clock it controls.
"""
from __future__ import annotations

import time as real_time
import types

import pytest

START = 1_780_000_000.0


class Clock:
    def __init__(self):
        self.now = START

    def time(self):
        return self.now

    @staticmethod
    def localtime(secs=None):
        return real_time.localtime(secs)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cog(load, clock, monkeypatch):
    module = load("cognition")
    # Only cognition's own `time` name is swapped, not the shared module.
    monkeypatch.setattr(module, "time", types.SimpleNamespace(
        time=clock.time, localtime=clock.localtime))
    module.reset()
    yield module
    module.reset()


def change(entity_id, value, *, device_class=None, unit=None, before=None):
    """A state_changed event as process() reads it."""
    def state(v):
        return types.SimpleNamespace(state=str(v), attributes={
            "device_class": device_class, "unit_of_measurement": unit})
    return types.SimpleNamespace(data={
        "entity_id": entity_id, "new_state": state(value),
        "old_state": None if before is None else state(before)})


# ── numeric readings ────────────────────────────────────────────────────────

@pytest.mark.parametrize("eid, dclass, unit, usual, spike", [
    ("sensor.lounge_temperature", "temperature", "°C", 21.0, 95.0),
    ("sensor.mains_voltage", "voltage", "V", 230.0, 900.0),
    ("sensor.server_load", None, None, 0.5, 40.0),
])
def test_a_numeric_spike_is_never_escalated(cog, clock, eid, dclass, unit, usual, spike):
    for _ in range(40):
        cog.process(change(eid, usual, device_class=dclass, unit=unit))
        clock.now += 60
    verdict = cog.process(change(eid, spike, device_class=dclass, unit=unit))
    assert verdict.escalate is False
    assert "spike" not in verdict.reason and "drift" not in verdict.reason
    assert cog.stats()["anomalies_escalated"] == 0


def test_numeric_statistics_are_not_saved(cog):
    for _ in range(40):
        cog.process(change("sensor.mains_voltage", 230.0, device_class="voltage", unit="V"))
    saved = cog._entry_to_dict(cog._MODEL["sensor.mains_voltage"])
    assert not {"n", "mean", "m2"} & set(saved)
    assert saved["last_state"] == "230.0"


def test_an_old_saved_entry_with_statistics_still_loads(cog):
    entry = cog._entry_from_dict({"first_seen": 1.0, "n": 40, "mean": 2.0, "m2": 9.0})
    assert entry.first_seen == 1.0
    assert entry.last_changed == 1.0 and entry.last_state == ""
    assert entry.hours == [0] * 24


# ── the cooldown for statistical escalations ───────────────────────────────

@pytest.fixture
def unusual(cog, monkeypatch):
    """Every event scores as a rare change at an unusual hour: the kind of
    escalation the cooldown is for."""
    monkeypatch.setattr(cog, "_salience",
                        lambda *a, **k: (0.7, "rare transition, unusual hour"))
    return cog


def test_a_noisy_entity_escalates_once_then_waits(unusual):
    first = unusual.process(change("switch.kettle", "on"))
    assert first.escalate is True and first.salience == 0.7
    assert unusual.process(change("switch.kettle", "off")).escalate is False
    assert unusual.stats()["anomalies_escalated"] == 1


def test_the_cooldown_is_kept_per_entity(unusual):
    assert unusual.process(change("switch.a", "on")).escalate is True
    assert unusual.process(change("switch.b", "on")).escalate is True


@pytest.mark.parametrize("waited, again", [
    (-1.0, False),   # one second short of the cooldown
    (0.0, True),     # exactly the cooldown
    (1.0, True),
])
def test_the_cooldown_runs_out(unusual, clock, waited, again):
    assert unusual.process(change("switch.kettle", "on")).escalate is True
    assert unusual.process(change("switch.kettle", "off")).escalate is False
    clock.now = START + unusual.ANOMALY_COOLDOWN + waited
    assert unusual.process(change("switch.kettle", "on")).escalate is again


def test_a_blocked_event_does_not_restart_the_cooldown(unusual, clock):
    unusual.process(change("switch.kettle", "on"))
    clock.now = START + unusual.ANOMALY_COOLDOWN / 2
    assert unusual.process(change("switch.kettle", "off")).escalate is False
    clock.now = START + unusual.ANOMALY_COOLDOWN
    assert unusual.process(change("switch.kettle", "on")).escalate is True


def test_a_below_threshold_event_is_not_escalated(cog, monkeypatch):
    monkeypatch.setattr(cog, "_salience", lambda *a, **k: (0.59, "rare transition"))
    assert cog.process(change("switch.kettle", "on")).escalate is False


# ── never throttled ─────────────────────────────────────────────────────────

def test_smoke_alarms_always_escalate(cog):
    verdicts = [cog.process(change(eid, "on", device_class="smoke")) for eid in (
        "binary_sensor.kitchen_smoke", "binary_sensor.hall_smoke",
        "binary_sensor.kitchen_smoke")]
    assert all(v.escalate for v in verdicts)
    assert "safety" in verdicts[0].reason
    assert verdicts[0].salience >= 0.9


@pytest.mark.parametrize("value", ["off", "unavailable", "unknown"])
def test_a_quiet_or_unreadable_smoke_sensor_is_not_a_trigger(cog, value):
    verdict = cog.process(change("binary_sensor.kitchen_smoke", value, device_class="smoke"))
    assert verdict.escalate is False
    assert "safety" not in verdict.reason


def test_a_triggered_alarm_panel_is_a_safety_trigger(cog):
    verdict = cog.process(change("alarm_control_panel.house", "triggered"))
    assert verdict.escalate is True and "safety" in verdict.reason


def test_access_points_skip_the_cooldown(cog, monkeypatch):
    # A lock alone scores 0.55; at an unusual hour it crosses the threshold
    # and, being an access point, is never held back.
    monkeypatch.setattr(cog, "_is_unusual_hour", lambda entry, now: True)
    verdicts = [cog.process(change("lock.front_door", state, before=prior))
                for state, prior in (("unlocked", "locked"), ("locked", "unlocked"),
                                     ("unlocked", "locked"))]
    assert [v.escalate for v in verdicts] == [True, True, True]
    assert verdicts[0].reason == "access-point, unusual hour"


def test_a_lock_coming_back_online_is_not_a_change(cog):
    verdict = cog.process(change("lock.front_door", "locked", before="unavailable"))
    assert verdict == (False, 0.0, "state recovery")
