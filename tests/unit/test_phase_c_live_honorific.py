"""Phase C: presence-aware honorific actually driving live announcements.

Phase A added the primitives (honorific.py); Phase B made every message-
building call site grammar-safe for an eventual empty honorific. Phase C is
the actual switch-over: ~30 fetch sites across the app now call
honorific.effective_honorific(hass) fresh at speak-time instead of a single
global config value — so a solo occupant hears their own configured
honorific (or the global default), and anyone else — multiple people home,
or nobody — hears none at all.

honorific.py's own logic (all_people/home_alone_entity_id/effective_honorific
in isolation) is already covered by test_honorific.py. These tests instead
exercise real announcement call sites end to end under 0/1/2-person
presence scenarios, proving the switch-over actually reaches them.
"""
import sys
import types

import pytest


def _set_nova_config(mod, monkeypatch, mapping):
    """Same convention as test_honorific.py / test_announce_resilience.py:
    monkeypatch the lazily-imported `nova_config` module in the same
    synthetic package as `mod`, since every consumer does `from . import
    nova_config` inside a function body, not at module level."""
    pkg = mod.__name__.rsplit(".", 1)[0]
    fake = types.SimpleNamespace(get=lambda k, d=None: mapping.get(k, d))
    monkeypatch.setitem(sys.modules, f"{pkg}.nova_config", fake)


# ── SafetyManager._check_freeze (cognitive_core.py) ──────────────────────────

@pytest.fixture
def safety(cognitive_core, fake_hass):
    return cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})


async def test_freeze_alert_uses_solo_persons_own_honorific(
    cognitive_core, safety, fake_hass, monkeypatch
):
    fake_hass.states.set("person.rachel", "home", friendly_name="Rachel")
    fake_hass.states.set("weather.home", "cloudy", temperature=15)
    _set_nova_config(cognitive_core, monkeypatch, {
        "person_honorifics": {"person.rachel": "boss"},
        "honorific": "sir",
    })
    action = await safety._check_freeze()
    assert action is not None
    assert action["message"].startswith("Boss,")


async def test_freeze_alert_falls_back_to_global_default_for_solo_person_without_override(
    cognitive_core, safety, fake_hass, monkeypatch
):
    fake_hass.states.set("person.jianna", "home", friendly_name="Jianna")
    fake_hass.states.set("weather.home", "cloudy", temperature=15)
    _set_nova_config(cognitive_core, monkeypatch, {"honorific": "boss"})
    action = await safety._check_freeze()
    assert action["message"].startswith("Boss,")


async def test_freeze_alert_drops_honorific_when_multiple_home(
    cognitive_core, safety, fake_hass, monkeypatch
):
    fake_hass.states.set("person.abi", "home", friendly_name="Abi")
    fake_hass.states.set("person.rachel", "home", friendly_name="Rachel")
    fake_hass.states.set("weather.home", "cloudy", temperature=15)
    _set_nova_config(cognitive_core, monkeypatch, {"honorific": "sir"})
    action = await safety._check_freeze()
    assert not action["message"].startswith(("Sir", "Boss"))
    assert action["message"].startswith("Outdoor temperature")


async def test_freeze_alert_drops_honorific_when_nobody_home(
    cognitive_core, safety, fake_hass, monkeypatch
):
    fake_hass.states.set("weather.home", "cloudy", temperature=15)
    _set_nova_config(cognitive_core, monkeypatch, {"honorific": "sir"})
    action = await safety._check_freeze()
    assert action["message"].startswith("Outdoor temperature")


# ── LockdownManager.engage (cognitive_core.py) ───────────────────────────────

@pytest.fixture
def lockdown(cognitive_core, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(cognitive_core, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))
    return cognitive_core.LockdownManager(fake_hass, {"honorific": "sir"})


async def test_lockdown_engage_uses_solo_persons_own_honorific(
    cognitive_core, lockdown, fake_hass, monkeypatch
):
    fake_hass.states.set("person.rachel", "home", friendly_name="Rachel")
    fake_hass.states.set("lock.front", "locked")
    _set_nova_config(cognitive_core, monkeypatch, {
        "person_honorifics": {"person.rachel": "boss"},
        "honorific": "sir",
    })
    action = await lockdown.engage("test")
    fake_hass.close_pending()
    assert action["message"].startswith("Boss,")


async def test_lockdown_engage_drops_honorific_when_multiple_home(
    cognitive_core, lockdown, fake_hass, monkeypatch
):
    fake_hass.states.set("person.abi", "home", friendly_name="Abi")
    fake_hass.states.set("person.rachel", "home", friendly_name="Rachel")
    fake_hass.states.set("lock.front", "locked")
    _set_nova_config(cognitive_core, monkeypatch, {"honorific": "sir"})
    action = await lockdown.engage("test")
    fake_hass.close_pending()
    # Still a complete, correctly-capitalized sentence — just no address.
    assert action["message"][0].isupper()
    assert not action["message"].startswith(("Sir,", "Boss,"))


# NOTE: sentinel.py and proactive_audio.py's own Phase C changes
# (NovaSentinel._live_honorific, proactive_audio._resolve_honorific) aren't
# covered here — both modules import `homeassistant.helpers.event`, which
# this test harness's HA stub doesn't provide (`_load()` fails at import
# time), a pre-existing gap that predates this change and already left both
# files with zero direct unit coverage (same class of gap as camera.py's
# `homeassistant.components.camera` stub miss). Verified instead by manual
# review against the same effective_honorific() contract exercised above,
# plus scripts/audit.py's static import/name resolution and the full
# pytest run, both clean.
