"""Assist policy bridge against Home Assistant's REAL Assist API (PHACC).

The unit tests (tests/unit/test_assist_policy.py) prove the adapter's logic
with fakes. What only a real core can prove is that the adapter reads Home
Assistant's actual tool objects correctly: IntentTool's intent_type, the
registered intent handlers, the target matcher and Assist exposure. These
tests build the real "assist" API instance and classify real HassTurnOn /
HassTurnOff calls against in-memory entity states.

No device is touched: the entities are plain states set in the test hass,
Nova's confirm_gate is a spy, and hass_api.async_call_tool is never called
for a protected action.
"""
import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component


async def _assist_api(hass, device_id=None):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "intent", {})
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Front door"})
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen"})
    hass.states.async_set("cover.garage_door", "closed", {"friendly_name": "Garage door"})
    for eid in ("lock.front_door", "light.kitchen", "cover.garage_door"):
        async_expose_entity(hass, "conversation", eid, True)
    ctx = llm.LLMContext(
        platform="nova", context=Context(user_id="user-1"), language="en",
        assistant="conversation", device_id=device_id,
    )
    return await llm.async_get_api(hass, "assist", ctx)


@pytest.fixture
def gate(monkeypatch):
    from custom_components.nova import policy
    calls = []

    async def _confirm_gate(hass, domain, service, entity_id="", action_label="", device_id=""):
        calls.append((domain, service, entity_id))
        if (domain, service) in {("lock", "unlock"), ("cover", "open_cover")}:
            return False, "not confirmed", "rejected"
        return True, "", "not_required"

    monkeypatch.setattr(policy, "confirm_gate", _confirm_gate)
    return calls


def _tool_name(api, intent_type):
    # Newer cores carry intent_type; 2026.2 dispatches the tool's own name.
    for tool in api.tools:
        if type(tool).__name__ == "IntentTool" and (
                getattr(tool, "intent_type", None) or tool.name) == intent_type:
            return tool.name
    raise AssertionError(f"{intent_type} not offered by the Assist API")


async def test_real_turn_off_on_lock_is_an_unlock(hass, gate):
    from custom_components.nova import assist_policy
    api = await _assist_api(hass)
    name = _tool_name(api, "HassTurnOff")
    d = await assist_policy.async_authorize(hass, api, name, {"name": "Front door"})
    assert gate == [("lock", "unlock", "lock.front_door")]
    assert not d.allowed
    assert d.classification.targets[0].risk == "high"


async def test_real_turn_on_cover_is_open_cover(hass, gate):
    from custom_components.nova import assist_policy
    api = await _assist_api(hass)
    name = _tool_name(api, "HassTurnOn")
    d = await assist_policy.async_authorize(hass, api, name, {"name": "Garage door"})
    assert gate == [("cover", "open_cover", "cover.garage_door")]
    assert not d.allowed


async def test_real_light_is_allowed(hass, gate):
    from custom_components.nova import assist_policy
    api = await _assist_api(hass)
    name = _tool_name(api, "HassTurnOn")
    d = await assist_policy.async_authorize(hass, api, name, {"name": "Kitchen"})
    assert gate == [("light", "turn_on", "light.kitchen")]
    assert d.allowed


async def test_real_unexposed_entity_is_not_resolved(hass, gate):
    from custom_components.nova import assist_policy
    api = await _assist_api(hass)
    hass.states.async_set("lock.back_door", "locked", {"friendly_name": "Back door"})
    async_expose_entity(hass, "conversation", "lock.back_door", False)
    name = _tool_name(api, "HassTurnOff")
    d = await assist_policy.async_authorize(hass, api, name, {"name": "Back door"})
    assert not d.allowed and gate == []


async def test_real_read_only_tools_pass(hass, gate):
    from custom_components.nova import assist_policy
    api = await _assist_api(hass)
    read_only = [t for t in api.tools
                 if type(t).__name__ in ("GetLiveContextTool", "GetDateTimeTool")]
    assert read_only
    for tool in read_only:
        d = await assist_policy.async_authorize(hass, api, tool.name, {})
        assert d.allowed and d.classification.kind == assist_policy.READ_ONLY
    assert gate == []
