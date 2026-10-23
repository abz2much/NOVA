"""The local fast path obeys the same authorisation gate as the agent (v7.44.0).

local_engine.try_local answers simple commands before any model is asked. It
must never become a second, unguarded way to unlock a door, open a garage or
run a scene: with confirmation switched on those requests go to the agent
(which asks), and with it off they run straight away, exactly as the agent
would run them.
"""
from __future__ import annotations

import sys
import types

import pytest

from conftest import readable_devices
from fakes import FakeHass

# What the stand-in confirmation module treats as needing a yes.
GUARDED_PAIRS = frozenset({
    ("lock", "unlock"), ("cover", "open_cover"), ("cover", "open"),
    ("alarm_control_panel", "alarm_disarm"),
})
SATELLITE = "satellite-kitchen"


@pytest.fixture
def engine(load):
    return load("local_engine")


@pytest.fixture
def confirmation(monkeypatch):
    """Install a stand-in voice_confirm. Call with on=True for "confirmation
    enabled" (guarded pairs then need a yes; nothing else ever does), or
    broken=True for a module whose protection lookup raises."""

    def _install(on: bool = True, *, broken: bool = False):
        stand_in = types.ModuleType("jc.voice_confirm")

        def action_is_protected(hass, domain, service, entity_id=""):
            if broken:
                raise RuntimeError("confirmation settings unreadable")
            return on and (domain, service) in GUARDED_PAIRS

        async def confirm(hass, question, entity_id=""):
            return False

        stand_in.action_is_protected = action_is_protected
        stand_in.confirm = confirm
        stand_in.is_voice_satellite_device = (
            lambda hass, device_id, strict=False: device_id == SATELLITE)
        monkeypatch.setitem(sys.modules, "jc.voice_confirm", stand_in)

    return _install


@pytest.fixture
def scene_gate(load, monkeypatch):
    """Answer policy.requires_confirmation with a fixed verdict."""
    policy = load("policy")

    def _set(needs_yes: bool):
        monkeypatch.setattr(policy, "requires_confirmation",
                            lambda hass, d, s, e="", *a, **k: needs_yes)

    return _set


def _readable_home() -> FakeHass:
    """A lock and a cover with readable states, for the live check (8.46.0)."""
    home = FakeHass()
    readable_devices(home, "lock.front", "cover.garage")
    return home


def _home_with(entity_id: str, state: str, name: str) -> FakeHass:
    home = FakeHass()
    home.states.set(entity_id, state, friendly_name=name)
    return home


# ── which service an action name means ──────────────────────────────────────

@pytest.mark.parametrize("action, entity_id, expected", [
    ("unlock", "lock.front", ("lock", "unlock")),
    ("open", "cover.garage", ("cover", "open_cover")),
    ("turn_off", "light.kitchen", ("light", "turn_off")),
    ("turn_on", "switch.pump", ("switch", "turn_on")),
    ("toggle", "fan.bedroom", ("fan", "toggle")),
    ("media_pause", "media_player.tv", ("media_player", "media_pause")),
    ("media_next", "media_player.tv", ("media_player", "media_next_track")),
    ("set_temp", "climate.hall", ("climate", "set_temperature")),
    ("volume_set", "media_player.tv", ("media_player", "volume_set")),
    ("greeting", "", None),
    ("time_query", "", None),
])
def test_action_to_service(engine, action, entity_id, expected):
    assert engine._service_for(action, entity_id) == expected


# ── whether an action has to go to the agent ────────────────────────────────

@pytest.mark.parametrize("enabled, action, entity_id, expected", [
    (True, "unlock", "lock.front", True),
    (True, "open", "cover.garage", True),
    (False, "unlock", "lock.front", False),
    (False, "open", "cover.garage", False),
    (True, "greeting", "", False),          # nothing to actuate, nothing to gate
    (True, "turn_off", "light.kitchen", False),
])
def test_needs_confirmation(engine, confirmation, enabled, action, entity_id, expected):
    confirmation(on=enabled)
    home = _readable_home()
    home.states.set("light.kitchen", "on")
    assert engine._needs_confirmation(home, action, entity_id) is expected


def test_satellite_unlock_needs_confirmation_even_when_disabled(engine, confirmation):
    # A voice can be imitated: from a satellite an unlock always defers.
    confirmation(on=False)
    home = _readable_home()
    assert engine._needs_confirmation(home, "unlock", "lock.front", SATELLITE) is True
    assert engine._needs_confirmation(home, "unlock", "lock.front", "phone-app") is False


def test_satellite_light_command_stays_ungated(engine, confirmation):
    confirmation(on=False)
    home = _home_with("light.kitchen", "on", "Kitchen")
    assert engine._needs_confirmation(home, "turn_off", "light.kitchen", SATELLITE) is False


def test_broken_confirmation_lookup_fails_closed_for_guarded(engine, confirmation):
    confirmation(broken=True)
    home = _readable_home()
    home.states.set("light.kitchen", "on")
    assert engine._needs_confirmation(home, "unlock", "lock.front") is True
    assert engine._needs_confirmation(home, "open", "cover.garage") is True
    assert engine._needs_confirmation(home, "turn_off", "light.kitchen") is False


def test_unreadable_lock_is_never_actuated_locally(engine, confirmation):
    # No state at all for the lock: the live check refuses, so it defers.
    confirmation(on=False)
    assert engine._needs_confirmation(FakeHass(), "unlock", "lock.nowhere") is True


# ── try_local end to end ────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase", ["unlock the front door", "unlock all doors"])
async def test_guarded_unlock_defers_when_enabled(engine, confirmation, phrase):
    confirmation(on=True)
    home = _home_with("lock.front_door", "locked", "Front Door")
    assert await engine.try_local(home, phrase, "sir") is None
    assert home.service_calls == []


async def test_unlock_runs_locally_when_disabled(engine, confirmation):
    confirmation(on=False)
    home = _home_with("lock.front_door", "locked", "Front Door")
    result = await engine.try_local(home, "unlock the front door", "sir")
    assert result is not None and result.handled
    assert home.service_calls == [("lock", "unlock", {"entity_id": "lock.front_door"})]
    home.close_pending()        # the background check it scheduled is not run


async def test_light_is_never_gated(engine, confirmation, load, monkeypatch):
    confirmation(on=True)
    # The light/switch/fan check polls through its own sleep seam; FakeHass
    # never changes the state, so without this the poll would really wait.
    waits: list = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(load("entity_verify"), "_SLEEP", no_wait)
    home = _home_with("light.kitchen", "on", "Kitchen")
    result = await engine.try_local(home, "turn off the kitchen light", "sir")
    assert result is not None and result.handled
    assert [call[:2] for call in home.service_calls] == [("light", "turn_off")]
    home.close_pending()


# ── scenes, scripts and the goodnight shortcut ──────────────────────────────
# A scene can unlock a door or disarm an alarm inside itself, so activating
# one locally goes through the same policy check as any other actuation.

SCENE_CASES = [
    pytest.param("scene.welcome", "scening", "Welcome", "activate welcome scene", id="activate"),
    pytest.param("scene.goodnight", "off", "Goodnight", "goodnight", id="goodnight"),
]


@pytest.mark.parametrize("entity_id, state, name, phrase", SCENE_CASES)
async def test_scene_defers_when_policy_wants_a_yes(engine, scene_gate,
                                                    entity_id, state, name, phrase):
    scene_gate(True)
    home = _home_with(entity_id, state, name)
    assert await engine.try_local(home, phrase, "sir") is None
    assert home.service_calls == []


@pytest.mark.parametrize("entity_id, state, name, phrase", SCENE_CASES)
async def test_scene_runs_when_policy_allows(engine, scene_gate,
                                             entity_id, state, name, phrase):
    scene_gate(False)
    home = _home_with(entity_id, state, name)
    result = await engine.try_local(home, phrase, "sir")
    assert result is not None and result.handled
    assert ("scene", "turn_on", {"entity_id": entity_id}) in home.service_calls
