"""Regression tests for the live-caught "Upstairs Thermo Lock" incident:

  1. A thermostat keypad/config lock (heatmiserneo's `lock.*` entity, which
     shares its device with a `climate.*` entity) was swept into sentinel's
     `lock_unlocked` rule purely because it's in the `lock` domain — HA's
     `lock` domain has no device_class to tell a door lock from a config
     lock apart, and this integration doesn't set entity_category either
     (confirmed live). Fixed by `_is_security_relevant_lock()` in
     sentinel.py, checked at every point sentinel resolves or matches a
     lock-domain entity.
  2. A brief integration reconnection (unavailable -> unlocked, confirmed
     live via HA's own recorder history: the lock AND its sibling climate/
     connectivity/standby entities on the same device all flipped within
     the same 30-second window) restarted `_state_start`'s duration timer
     as if the entity had "just" become unlocked, producing an announcement
     20 minutes later, then another 20 minutes after that.
  3. `_announce_rule` never checked quiet hours at all — it spoke through
     every configured speaker regardless of time. Fixed with a deterministic
     (sleep_detection.is_sleeping, never LLM/memory-driven) check that only
     a rule explicitly marked urgent_security=True (none of DEFAULT_RULES
     are) can bypass.
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def sentinel_mod(load, monkeypatch):
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    return load("sentinel")


class _FakeRegistryEntry:
    def __init__(self, entity_id, device_id=None, entity_category=None):
        self.entity_id = entity_id
        self.device_id = device_id
        self.entity_category = entity_category


class _FakeEntityRegistry:
    """Stands in for homeassistant.helpers.entity_registry's async_get(hass)
    result — only the surface _is_security_relevant_lock() actually calls."""
    def __init__(self, entries: dict[str, _FakeRegistryEntry], device_entities: dict[str, list]):
        self._entries = entries
        self._device_entities = device_entities

    def async_get(self, entity_id):
        return self._entries.get(entity_id)


@pytest.fixture
def fake_registries(sentinel_mod, monkeypatch):
    """Returns a setup(entries, device_entities) helper that wires
    sentinel_mod.er.async_get / async_entries_for_device to fake data,
    without touching a real Home Assistant entity registry."""
    def _setup(entries: dict, device_entities: dict):
        reg = _FakeEntityRegistry(entries, device_entities)
        monkeypatch.setattr(sentinel_mod.er, "async_get", lambda hass: reg)
        monkeypatch.setattr(
            sentinel_mod.er, "async_entries_for_device",
            lambda registry, device_id, include_disabled_entities=False:
                device_entities.get(device_id, []),
            raising=False,
        )
        return reg
    return _setup


# ── 1. Thermostat config lock is never classified security-relevant ────────

def test_thermostat_lock_sharing_device_with_climate_is_not_security_relevant(
    sentinel_mod, fake_hass, fake_registries,
):
    fake_registries(
        entries={
            "lock.upstairs_thermo_lock": _FakeRegistryEntry(
                "lock.upstairs_thermo_lock", device_id="dev1", entity_category=None),
        },
        device_entities={
            "dev1": [
                _FakeRegistryEntry("climate.upstairs_thermo", device_id="dev1"),
                _FakeRegistryEntry("lock.upstairs_thermo_lock", device_id="dev1"),
            ],
        },
    )
    assert sentinel_mod._is_security_relevant_lock(
        fake_hass, "lock.upstairs_thermo_lock") is False


def test_entity_category_config_is_also_respected_when_present(
    sentinel_mod, fake_hass, fake_registries,
):
    """Some integrations DO set entity_category correctly -- use it when
    available, per the v3-style "prefer verified metadata" instruction."""
    fake_registries(
        entries={
            "lock.some_config_lock": _FakeRegistryEntry(
                "lock.some_config_lock", device_id="dev2", entity_category="config"),
        },
        device_entities={"dev2": []},
    )
    assert sentinel_mod._is_security_relevant_lock(
        fake_hass, "lock.some_config_lock") is False


def test_a_real_door_lock_with_no_sibling_climate_entity_stays_security_relevant(
    sentinel_mod, fake_hass, fake_registries,
):
    fake_registries(
        entries={
            "lock.front_door": _FakeRegistryEntry(
                "lock.front_door", device_id="dev3", entity_category=None),
        },
        device_entities={
            "dev3": [_FakeRegistryEntry("lock.front_door", device_id="dev3")],
        },
    )
    assert sentinel_mod._is_security_relevant_lock(fake_hass, "lock.front_door") is True


def test_unknown_entity_fails_open_to_security_relevant(sentinel_mod, fake_hass, fake_registries):
    fake_registries(entries={}, device_entities={})
    assert sentinel_mod._is_security_relevant_lock(fake_hass, "lock.mystery") is True


# ── Test 1 (required list): thermostat lock unlocked creates no alert ──────

def test_thermostat_lock_unlocked_is_never_enrolled_for_the_lock_rule(
    sentinel_mod, fake_hass, fake_registries,
):
    fake_registries(
        entries={
            "lock.upstairs_thermo_lock": _FakeRegistryEntry(
                "lock.upstairs_thermo_lock", device_id="dev1"),
            "lock.front_door": _FakeRegistryEntry("lock.front_door", device_id="dev3"),
        },
        device_entities={
            "dev1": [_FakeRegistryEntry("climate.upstairs_thermo", device_id="dev1")],
            "dev3": [],
        },
    )
    fake_hass.states.set("lock.upstairs_thermo_lock", "unlocked",
                          friendly_name="Upstairs Thermo Lock")
    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")

    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=sentinel_mod.DEFAULT_RULES, entry=None)
    ids = s._collect_entity_ids()

    assert "lock.front_door" in ids
    assert "lock.upstairs_thermo_lock" not in ids


def test_entity_matches_rule_also_rejects_the_thermostat_lock(
    sentinel_mod, fake_hass, fake_registries,
):
    """Defense in depth: even if a stale/manually-populated entity_cache
    somehow still contained the thermostat lock, _entity_matches_rule()
    independently rejects it for a requires_security_lock rule."""
    fake_registries(
        entries={"lock.upstairs_thermo_lock": _FakeRegistryEntry(
            "lock.upstairs_thermo_lock", device_id="dev1")},
        device_entities={"dev1": [_FakeRegistryEntry("climate.upstairs_thermo", device_id="dev1")]},
    )
    rule = next(r for r in sentinel_mod.DEFAULT_RULES if r["id"] == "lock_unlocked")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[rule], entry=None)
    assert s._entity_matches_rule("lock.upstairs_thermo_lock", rule) is False


# ── Test 2 (required list): unavailable -> unlocked reconnection ───────────

def test_reconnection_from_unavailable_does_not_immediately_restart_the_clock_wrongly(
    sentinel_mod, fake_hass, fake_registries,
):
    """A real door lock: going unavailable then back to unlocked IS treated
    as a fresh "unlocked" state (matches real hardware behaviour -- a lock
    that reconnects while genuinely unlocked should still eventually alert)
    -- the fix for the reported incident is entity CLASSIFICATION (test
    above), not suppressing every reconnection duration reset generically.
    This test pins down that a real lock's state machine is unaffected: the
    duration key is present after the transition, exactly as before."""
    fake_registries(
        entries={"lock.front_door": _FakeRegistryEntry("lock.front_door", device_id="dev3")},
        device_entities={"dev3": []},
    )
    rule = next(r for r in sentinel_mod.DEFAULT_RULES if r["id"] == "lock_unlocked")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[rule], entry=None)

    class _Event:
        def __init__(self, entity_id, state):
            self.data = {"entity_id": entity_id, "new_state": types.SimpleNamespace(state=state)}

    s._handle_state_change(_Event("lock.front_door", "unavailable"))
    key = "lock.front_door:lock_unlocked"
    assert key not in s._state_start  # correctly cleared while unavailable

    s._handle_state_change(_Event("lock.front_door", "unlocked"))
    assert key in s._state_start  # a real lock is legitimately tracked again


# ── Tests 3/4 (required list): no 20/40-min repeats, cannot speak overnight ─

def test_thermostat_lock_excluded_from_cache_never_schedules_duration_checks(
    sentinel_mod, fake_hass, fake_registries,
):
    fake_registries(
        entries={"lock.upstairs_thermo_lock": _FakeRegistryEntry(
            "lock.upstairs_thermo_lock", device_id="dev1")},
        device_entities={"dev1": [_FakeRegistryEntry("climate.upstairs_thermo", device_id="dev1")]},
    )
    fake_hass.states.set("lock.upstairs_thermo_lock", "unlocked",
                          friendly_name="Upstairs Thermo Lock")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=sentinel_mod.DEFAULT_RULES, entry=None)
    s._entity_cache = s._collect_entity_ids()

    class _Event:
        def __init__(self, entity_id, state):
            self.data = {"entity_id": entity_id, "new_state": types.SimpleNamespace(state=state)}

    s._handle_state_change(_Event("lock.upstairs_thermo_lock", "unlocked"))
    # never enrolled -> never matches -> no duration key was ever created,
    # so _check_durations has nothing to fire on at the 20 or 40 minute mark
    assert not any(k.startswith("lock.upstairs_thermo_lock:") for k in s._state_start)


async def test_announce_rule_does_not_speak_overnight_for_a_non_urgent_rule(
    sentinel_mod, fake_hass, monkeypatch,
):
    sd = load_sleep_detection_stub(sentinel_mod, monkeypatch, sleeping=True)
    announced = []
    async def fake_announce(hass, text, tts, speakers, context="", action_request_id=None):
        announced.append(text)
    monkeypatch.setattr(sentinel_mod, "async_announce", fake_announce)
    monkeypatch.setattr(sentinel_mod, "save_sentinel_event", lambda *a, **k: None)
    monkeypatch.setattr(sentinel_mod, "save_message", lambda *a, **k: None)

    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[], entry=None)
    rule = {"id": "lock_unlocked", "message": "{honorific}, {friendly_name} has been unlocked for {minutes} minutes."}

    await s._announce_rule("lock.front_door", rule, 20)

    assert announced == []  # speaker never called while sleeping


async def test_announce_rule_still_speaks_when_awake(sentinel_mod, fake_hass, monkeypatch):
    load_sleep_detection_stub(sentinel_mod, monkeypatch, sleeping=False)
    announced = []
    async def fake_announce(hass, text, tts, speakers, context="", action_request_id=None):
        announced.append(text)
    monkeypatch.setattr(sentinel_mod, "async_announce", fake_announce)
    monkeypatch.setattr(sentinel_mod, "save_sentinel_event", lambda *a, **k: None)
    monkeypatch.setattr(sentinel_mod, "save_message", lambda *a, **k: None)

    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[], entry=None)
    rule = {"id": "lock_unlocked", "message": "{honorific}, {friendly_name} has been unlocked for {minutes} minutes."}

    await s._announce_rule("lock.front_door", rule, 20)

    assert len(announced) == 1


async def test_a_generic_high_urgency_label_alone_does_not_bypass_quiet_hours(
    sentinel_mod, fake_hass, monkeypatch,
):
    """Only urgent_security=True (not present on any DEFAULT_RULES entry)
    may bypass -- a rule merely being present in DEFAULT_RULES, or having a
    "security"-sounding id, is not enough on its own."""
    load_sleep_detection_stub(sentinel_mod, monkeypatch, sleeping=True)
    announced = []
    async def fake_announce(hass, text, tts, speakers, context="", action_request_id=None):
        announced.append(text)
    monkeypatch.setattr(sentinel_mod, "async_announce", fake_announce)
    monkeypatch.setattr(sentinel_mod, "save_sentinel_event", lambda *a, **k: None)
    monkeypatch.setattr(sentinel_mod, "save_message", lambda *a, **k: None)

    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                          friendly_name="Front Door")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[], entry=None)
    rule = {"id": "door_left_open", "message": "{honorific}, {friendly_name} has been open for {minutes} minutes."}
    await s._announce_rule("binary_sensor.front_door", rule, 10)
    assert announced == []


# ── Test 6 (required list): genuine urgent security event still speaks ─────

async def test_a_rule_explicitly_flagged_urgent_security_still_speaks_overnight(
    sentinel_mod, fake_hass, monkeypatch,
):
    load_sleep_detection_stub(sentinel_mod, monkeypatch, sleeping=True)
    announced = []
    async def fake_announce(hass, text, tts, speakers, context="", action_request_id=None):
        announced.append(text)
    monkeypatch.setattr(sentinel_mod, "async_announce", fake_announce)
    monkeypatch.setattr(sentinel_mod, "save_sentinel_event", lambda *a, **k: None)
    monkeypatch.setattr(sentinel_mod, "save_message", lambda *a, **k: None)

    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                          friendly_name="Front Door")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[], entry=None)
    rule = {"id": "confirmed_intrusion", "urgent_security": True,
            "message": "{honorific}, {friendly_name} — confirmed intrusion."}
    await s._announce_rule("binary_sensor.front_door", rule, 0)
    assert len(announced) == 1


# ── Test 8-adjacent: a real ignore_entity rule genuinely suppresses sentinel

def test_ignore_manager_rule_is_now_actually_checked_by_sentinel(
    sentinel_mod, fake_hass, monkeypatch,
):
    """The gap this closes: cognitive_core.IgnoreManager (what ignore_entity
    writes to) was never consulted by sentinel.py at all, so a genuine
    ignore_entity call had no real effect on sentinel's own alerts."""
    monkeypatch.setattr(sentinel_mod, "_is_ignored",
                         lambda hass, eid: eid == "lock.front_door")
    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
    fake_hass.states.set("lock.back_door", "unlocked", friendly_name="Back Door")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[{"id": "lock_unlocked", "domain": "lock",
                                           "state": "unlocked", "for_minutes": 20}],
                                   entry=None)
    ids = s._collect_entity_ids()
    assert "lock.front_door" not in ids
    assert "lock.back_door" in ids


def load_sleep_detection_stub(sentinel_mod, monkeypatch, *, sleeping: bool):
    import types as _types
    sd = _types.ModuleType("jc.sleep_detection")
    sd.is_sleeping = lambda hass, **kw: (sleeping, None)
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sd)
    return sd
