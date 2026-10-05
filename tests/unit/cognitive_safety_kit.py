"""Shared fixtures and helpers for the cognitive_core safety characterisation
tests (test_cognitive_safety_*.py, test_cognitive_lockdown_manager.py,
test_cognitive_loop_and_routing.py).

Not collected itself (no test_ prefix). These tests only pin what the code
does today, so a later split of cognitive_core.py is safe. They use the
existing fakes (FakeHass and FakeStates), a patched clock, and no real
network, sleeps or Home Assistant services.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture
def clock(cc, monkeypatch):
    """A controllable wall clock. time.time() is patched, so every module that
    reads it (intrusion call off windows, cooldowns) sees the same instant."""
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(cc.time, "time", lambda: t["now"])
    return t


@pytest.fixture(autouse=True)
def _isolated_core(cc, load, tmp_path, monkeypatch):
    """cognitive_core keeps one module level _CORE and a few module globals,
    and the loader caches the module across tests. Snapshot and restore them,
    point the lockdown, autonomy and ignore files at tmp_path, and clear the
    intrusion call off state."""
    core = cc._CORE
    saved = dict(core.__dict__)
    saved_log_ts = cc._ALARM_INDET_LOG_TS
    saved_pattern_last = dict(cc._PATTERN_LOG_LAST)
    monkeypatch.setattr(cc, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown_state.json"))
    monkeypatch.setattr(cc, "AUTONOMY_FILE", str(tmp_path / "autonomy_grants.json"))
    monkeypatch.setattr(cc, "IGNORE_FILE", str(tmp_path / "ignore_rules.json"))
    monkeypatch.setattr(cc, "PATTERNS_DB", str(tmp_path / "patterns.db"))
    core.hass = None
    core.config = {}
    core.running = False
    core.task = None
    core.unsub = None
    core.alarm_unsub = None
    core.ignore_mgr = None
    core.safety_mgr = None
    core.lockdown_mgr = None
    core.proactive_mgr = None
    core.autonomy_mgr = None
    core.state_logger = None
    core.automation_contexts = None
    core.entry = None
    core.tick_count = 0
    core.actions_taken = 0
    core.offers_made = 0
    core.autonomous_actions = 0
    core.last_tick = 0.0
    core.startup_time = 0.0
    core.pending_offer = None
    cc._ALARM_INDET_LOG_TS = 0.0
    cc._PATTERN_LOG_LAST.clear()
    intrusion = load("intrusion")
    intrusion.clear_calloff()
    yield
    intrusion.clear_calloff()
    core.__dict__.clear()
    core.__dict__.update(saved)
    cc._ALARM_INDET_LOG_TS = saved_log_ts
    cc._PATTERN_LOG_LAST.clear()
    cc._PATTERN_LOG_LAST.update(saved_pattern_last)


def intrusions(actions):
    return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]


def away(hass, door=True):
    """Residents confidently away. By default one exterior door is open, which
    is the corroboration the away branch asks for."""
    hass.states.set("person.username", "not_home")
    if door:
        hass.states.set("binary_sensor.front_door", "on", device_class="door")


def motion(hass, eid="binary_sensor.living_motion", on=True):
    hass.states.set(eid, "on" if on else "off", device_class="motion")


def service_calls(hass, domain, service):
    return [c for c in hass.service_calls if c[0] == domain and c[1] == service]
