"""Phase 3B Final: the remaining runtime consumers against a real Home
Assistant (PHACC).

After the real setup, each consumer's panel setting is put in the entry's
NovaRuntime.runtime_config, and the bridge's runtime_config is replaced with
different, drifted values. Every consumer must answer from the runtime. The
same consumers then fail with NovaRuntimeUnavailable while the loaded entry
has lost its runtime, and fall back to their defaults once it is unloaded.

Covers nova_config.runtime_get, the directive helper, camera, Sentinel,
solar, the TTS helper, audio routing, entity filtering, the alarm source,
the package monitor, the reasoning loop, the automation inventory, service
health, and the observer's owning entry.

Nothing is announced or sent: no speaker, lock, alarm or cover is operated.
"""
from contextlib import contextmanager

import pytest

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _no_real_config,
    _restore_nova_config,
    _setup,
)

RUNTIME = {
    "vision_model": "runtime/vision",
    "directive": "RUNTIME DIRECTIVE: keep the household safe.",
    "tts_engine": "tts.runtime_voice",
    "tts_use_ha_voice": True,
    "movie_media_player": "media_player.projector",
    "excluded_entities": ["light.den"],
    "security_alarm_entity": "alarm_control_panel.runtime",
    "announcements_enabled": False,
    "rich_reasoning": True,
    "energy_cost_today_entity": "sensor.cost_runtime",
}
DRIFTED = {
    "vision_model": "bridge/vision",
    "directive": "BRIDGE DIRECTIVE",
    "tts_engine": "tts.bridge_voice",
    "tts_use_ha_voice": False,
    "movie_media_player": "media_player.speaker",
    "excluded_entities": ["light.kitchen"],
    "security_alarm_entity": "alarm_control_panel.bridge",
    "announcements_enabled": True,
    "rich_reasoning": False,
    "energy_cost_today_entity": "sensor.cost_bridge",
}


@contextmanager
def _runtime_missing(entry):
    """Simulate a loaded entry that lost its runtime, then put it back."""
    runtime = entry.runtime_data
    object.__delattr__(entry, "runtime_data")
    try:
        yield runtime
    finally:
        entry.runtime_data = runtime


def _states(hass):
    for eid, state in (("tts.runtime_voice", "unknown"), ("tts.bridge_voice", "unknown"),
                       ("media_player.speaker", "idle"), ("media_player.projector", "idle"),
                       ("sensor.cost_runtime", "1.25"), ("sensor.cost_bridge", "9.99")):
        hass.states.async_set(eid, state)


def _probes(hass, entry):
    """name -> zero-argument call returning the consumer's answer."""
    from custom_components.nova import (
        alarm_source, audio_routing, automation_inventory, camera, directive_helper,
        entity_filter, nova_config, package_monitor, reasoning_loop, tts_helper,
    )
    from custom_components.nova.diagnostics import service_health

    return {
        "runtime_get": lambda: nova_config.runtime_get(hass, entry, "vision_model", "d"),
        "camera": lambda: camera._cfg_opt(hass, "vision_model", "d"),
        "directive": lambda: directive_helper.build_system_prompt(
            hass, "sir").split("\n\n---\n\n")[0],
        "sentinel": lambda: _sentinel_tts(hass, entry),
        "tts_helper": lambda: tts_helper.tts_use_ha_voice(hass),
        "audio_routing": lambda: audio_routing.drop_display_targets(
            hass, ["media_player.speaker", "media_player.projector"], "test"),
        "entity_filter": lambda: entity_filter.is_excluded(hass, "light.den"),
        "alarm_source": lambda: alarm_source._configured(hass, None),
        "package_monitor": lambda: package_monitor._announcements_on(hass),
        "reasoning_loop": lambda: reasoning_loop._rich_mode(hass),
        "automation_inventory": lambda: automation_inventory.get_inventory(hass),
        "service_health": lambda: service_health._check_scheduler(hass)["status"],
    }


def _sentinel_tts(hass, entry):
    from custom_components.nova.sentinel import NovaSentinel
    return NovaSentinel(hass, None, "sir", entry=entry)._tts_entity()


async def _loaded_with_drifted_bridge(hass):
    entry = await _setup(hass)
    _states(hass)
    entry.runtime_data.runtime_config.update(RUNTIME)
    hass.data[DOMAIN][entry.entry_id]["runtime_config"] = dict(DRIFTED)
    return entry


async def test_consumers_answer_from_the_runtime(hass):
    from custom_components.nova import solar
    from custom_components.nova.const import get_directive
    entry = await _loaded_with_drifted_bridge(hass)
    runtime = entry.runtime_data
    got = {name: call() for name, call in _probes(hass, entry).items()}
    assert got == {
        "runtime_get": "runtime/vision",
        "camera": "runtime/vision",
        "directive": get_directive("", RUNTIME["directive"]),
        "sentinel": "tts.runtime_voice",
        "tts_helper": True,
        "audio_routing": ["media_player.speaker"],
        "entity_filter": True,
        "alarm_source": "alarm_control_panel.runtime",
        "package_monitor": False,
        "reasoning_loop": True,
        "automation_inventory": runtime.automation_inventory,
        "service_health": got["service_health"],
    }
    assert got["service_health"] in ("ok", "warn")     # the runtime's scheduler
    cost, source = await solar._cost_today(hass, None, None)
    assert cost["today"] == 1.25


async def test_live_runtime_change_is_seen_next_time(hass):
    entry = await _loaded_with_drifted_bridge(hass)
    probes = _probes(hass, entry)
    rc = entry.runtime_data.runtime_config
    rc["vision_model"] = "runtime/second"
    rc["excluded_entities"] = []
    rc["rich_reasoning"] = False
    assert probes["runtime_get"]() == "runtime/second"
    assert probes["camera"]() == "runtime/second"
    assert probes["entity_filter"]() is False
    assert probes["reasoning_loop"]() is False


async def test_directive_without_runtime_value_is_unchanged(hass):
    """No runtime directive: the prompt is exactly what the entry-only
    resolution gives (text, structure and persona unchanged)."""
    from custom_components.nova import directive_helper
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc.pop("directive", None)
    rc.pop("directive_preset", None)
    hass.data[DOMAIN][entry.entry_id]["runtime_config"] = {"directive": "BRIDGE DIRECTIVE"}
    prompt = directive_helper.build_system_prompt(hass, "sir", "task")
    assert prompt.startswith(directive_helper.resolve_directive(entry))
    assert "BRIDGE DIRECTIVE" not in prompt
    assert prompt.endswith("task")


@pytest.mark.parametrize("name", [
    "runtime_get", "camera", "directive", "tts_helper", "audio_routing",
    "entity_filter", "alarm_source", "package_monitor", "reasoning_loop",
    "automation_inventory", "service_health", "sentinel"])
async def test_loaded_entry_without_runtime_fails_visibly(hass, name):
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _loaded_with_drifted_bridge(hass)
    probe = _probes(hass, entry)[name]
    with _runtime_missing(entry):
        with pytest.raises(NovaRuntimeUnavailable):
            probe()


async def test_solar_without_runtime_fails_visibly(hass):
    from custom_components.nova import solar
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    entry = await _loaded_with_drifted_bridge(hass)
    with _runtime_missing(entry):
        with pytest.raises(NovaRuntimeUnavailable):
            await solar._cost_today(hass, None, None)


async def test_consumers_are_safe_after_unload(hass):
    from custom_components.nova import solar
    entry = await _loaded_with_drifted_bridge(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # A stale bridge left behind would still be ignored.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"runtime_config": dict(DRIFTED)}
    try:
        got = {name: call() for name, call in _probes(hass, entry).items()
               if name not in ("runtime_get", "camera", "directive", "sentinel")}
    finally:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    assert got == {
        "tts_helper": False,
        "audio_routing": ["media_player.speaker", "media_player.projector"],
        "entity_filter": False,
        "alarm_source": "",
        "package_monitor": True,
        "reasoning_loop": False,
        "automation_inventory": None,
        "service_health": "off",
    }
    cost, _source = await solar._cost_today(hass, None, None)
    assert cost is None or cost.get("today") != 9.99


async def test_observer_is_started_for_the_loaded_entry(hass, monkeypatch):
    """The observer_start service hands the observer its owning entry."""
    from custom_components.nova import cognitive_core, observer
    seen = []

    async def _start(hass_, config, entry=None):
        seen.append(entry)
        observer._STATE.running = True

    async def _stop():
        await cognitive_core.stop()
        observer._STATE.running = False

    monkeypatch.setattr(observer._STATE, "running", False)
    monkeypatch.setattr(observer, "start", _start)
    monkeypatch.setattr(observer, "stop", _stop)
    entry = await _setup(hass)
    await hass.services.async_call(DOMAIN, "observer_start", {}, blocking=True)
    assert seen == [entry]
    await hass.services.async_call(DOMAIN, "observer_stop", {}, blocking=True)
