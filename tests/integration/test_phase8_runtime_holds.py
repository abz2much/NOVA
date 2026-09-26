"""Phase 8 review amendment against a real Home Assistant (PHACC).

* "Can you reach Home Assistant?" is answered from the real CoreState: a
  running instance is healthy, every other state says what it is doing.
  The real enum has lowercase names and UPPERCASE values, which lowercase
  string fakes cannot show.
* The provider holds belong to the loaded entry's NovaRuntime: one runtime
  keeps a hold, unload clears it, and a reload starts with a new, empty set.

Nothing is actuated: no provider is reached and no device is touched.
"""
import pytest
from homeassistant.core import CoreState

from .test_lifecycle_cleanup import _restore_nova_config as _restore  # noqa: F401
from .test_lifecycle_cleanup import _setup

_restore_nova_config = _restore   # autouse: keep the shared config.json as it was

PLATFORM_QUESTION = "Can you reach Home Assistant?"


async def _ask(hass, text=PLATFORM_QUESTION):
    from custom_components.nova import local_engine
    return await local_engine.try_local(hass, text)


async def test_a_real_running_instance_is_reported_healthy(hass):
    assert hass.state is CoreState.running
    hass.states.async_set("sensor.test_temperature", "21")
    res = await _ask(hass)
    assert res is not None and res.success
    assert "is running and responding" in res.text
    assert "can see 1 entity." in res.text
    assert "right now" not in res.text


@pytest.mark.parametrize(("state", "word"), [
    (CoreState.not_running, "not running"),
    (CoreState.starting, "starting"),
    (CoreState.stopping, "stopping"),
    (CoreState.final_write, "shutting down"),
    (CoreState.stopped, "stopped"),
])
async def test_a_real_non_running_state_is_reported_as_such(hass, state, word):
    from custom_components.nova import local_engine
    hass.set_state(state)
    try:
        text = local_engine._platform_status(hass, "")
    finally:
        hass.set_state(CoreState.running)
    assert f"is {word} right now" in text
    assert "running and responding" not in text


async def test_a_presence_question_still_answers_presence(hass):
    res = await _ask(hass, "Who is home?")
    assert res is not None
    assert "Home Assistant" not in res.text


async def test_holds_are_owned_by_the_runtime_and_reset_by_reload(hass):
    from custom_components.nova.cognitive.coordinator import _provider_holds
    from custom_components.nova.runtime import clear_runtime

    entry = await _setup(hass)
    first = entry.runtime_data.provider_holds
    assert _provider_holds(hass) is first and len(first) == 0
    first.hold("sig-a")
    assert _provider_holds(hass).held("sig-a")          # same loaded runtime

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    second = entry.runtime_data.provider_holds
    assert second is not first
    assert len(first) == 0                               # unload cleared the old one
    assert not second.held("sig-a") and _provider_holds(hass) is second

    second.hold("sig-b")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(second) == 0 and not hasattr(entry, "runtime_data")
    assert _provider_holds(hass) is None                 # nothing module-level remains
    clear_runtime(entry)                                 # idempotent
