"""Cognition noise control — v7.49.0.

No numeric reading is escalated on statistics alone (8.40.0: 100 of 104
rated alerts were unnecessary, many of them voltage and server load spikes).
No single noisy entity escalates more than once per cooldown window.
Safety/access triggers are never throttled.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def cognition(load):
    c = load("cognition")
    c.reset()
    return c


class _State:
    def __init__(self, state, attributes=None):
        self.state = str(state)
        self.attributes = attributes or {}


class _Event:
    def __init__(self, entity_id, new_state, old_state=None):
        self.data = {"entity_id": entity_id, "new_state": new_state, "old_state": old_state}


def _ev(entity_id, value, dclass=None, unit=None):
    return _Event(entity_id, _State(value, {"device_class": dclass, "unit_of_measurement": unit}))


def _warm(cognition, entity_id, value, dclass=None, unit=None, n=40):
    for _ in range(n):
        cognition.process(_ev(entity_id, value, dclass, unit))


# ── numeric readings are never escalated on statistics alone (8.40.0) ──────
@pytest.mark.parametrize("eid,dclass,unit,normal,spike", [
    ("sensor.living_room_temperature", "temperature", "°F", 70.0, 200.0),
    ("sensor.mains_voltage", "voltage", "V", 230.0, 900.0),
    ("sensor.server_load", None, None, 0.5, 40.0),
])
def test_numeric_spike_is_never_escalated(cognition, eid, dclass, unit, normal, spike):
    _warm(cognition, eid, normal, dclass, unit)
    d = cognition.process(_ev(eid, spike, dclass, unit))
    assert d.escalate is False
    assert "spike" not in d.reason and "drift" not in d.reason


def test_numeric_stats_are_not_persisted(cognition):
    _warm(cognition, "sensor.mains_voltage", 230.0, "voltage", "V")
    saved = cognition._entry_to_dict(cognition._MODEL["sensor.mains_voltage"])
    assert not {"n", "mean", "m2"} & set(saved)


def test_old_saved_entries_with_numeric_stats_still_load(cognition):
    e = cognition._entry_from_dict({"first_seen": 1.0, "n": 40, "mean": 2.0, "m2": 9.0})
    assert e.first_seen == 1.0


# ── per-entity cooldown for escalations with no safety or access driver ─────
@pytest.fixture
def unusual(cognition, monkeypatch):
    """Every event scores as an unusual-hour change, the kind of escalation
    the cooldown exists for."""
    monkeypatch.setattr(cognition, "_salience",
                        lambda *a, **k: (0.7, "rare transition, unusual hour"))
    return cognition


def test_noisy_entity_escalates_once_then_cools_down(unusual):
    assert unusual.process(_ev("switch.kettle", "on")).escalate is True
    assert unusual.process(_ev("switch.kettle", "off")).escalate is False


def test_cooldown_is_per_entity(unusual):
    assert unusual.process(_ev("switch.a", "on")).escalate is True
    assert unusual.process(_ev("switch.b", "on")).escalate is True


def test_cooldown_expires(unusual, monkeypatch):
    import time as _t
    base = _t.time()
    monkeypatch.setattr(unusual.time, "time", lambda: base)
    assert unusual.process(_ev("switch.kettle", "on")).escalate is True
    assert unusual.process(_ev("switch.kettle", "off")).escalate is False
    monkeypatch.setattr(unusual.time, "time",
                        lambda: base + unusual.ANOMALY_COOLDOWN + 1)
    assert unusual.process(_ev("switch.kettle", "on")).escalate is True


# ── safety is never throttled ────────────────────────────────────────────────
def test_safety_bypasses_cooldown(cognition):
    d1 = cognition.process(_ev("binary_sensor.kitchen_smoke", "on", "smoke"))
    d2 = cognition.process(_ev("binary_sensor.hall_smoke", "on", "smoke"))
    d3 = cognition.process(_ev("binary_sensor.kitchen_smoke", "on", "smoke"))
    assert d1.escalate and d2.escalate and d3.escalate
    assert "safety" in d1.reason
