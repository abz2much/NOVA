"""Nova's public contracts against a real Home Assistant (PHACC).

The unit twin (tests/unit/test_public_contracts.py) pins the contracts from
source against tests/fixtures/contracts/. This file proves the same fixtures
describe what a real Home Assistant actually registers: service schemas,
nova/* WebSocket schemas and admin gates, representative WebSocket response
shapes, the conversation entity's identity, and the stable imports other
modules rely on. An intentional contract change must update the
implementation, its migration or compatibility handling, and the fixture
together.

Nothing here actuates a device or reaches a provider: WebSocket calls are
read-only status commands, and the one service call patches its handler out.
"""
import json
import os
import pathlib
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import DOMAIN, _make_entry

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "contracts"


def _fixture(name):
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


async def _setup(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _schema_keys(schema) -> dict:
    keys = {}
    if schema is False or schema is None:   # registered without fields
        return keys
    for k in (schema.schema if isinstance(schema, vol.Schema) else schema):
        if isinstance(k, vol.Marker):
            keys[str(k.schema)] = "required" if isinstance(k, vol.Required) else "optional"
        else:
            keys[str(k)] = "required"
    return keys


async def test_registered_services_match_contract(hass):
    await _setup(hass)
    pinned = _fixture("services")
    registered = hass.services.async_services()[DOMAIN]
    assert set(registered) == set(pinned["documented"])
    for name, keys in pinned["registered_schemas"].items():
        schema = registered[name].schema
        if keys is None:
            assert schema is None, f"{name} gained a schema"
        else:
            assert _schema_keys(schema) == keys, f"service {name} schema changed"


async def _analyze_camera_calls(hass, data):
    """Call nova.analyze_camera with the analysis handler patched out (no
    camera, vision provider or speaker is touched) and return what reached it."""
    seen = []

    async def _record(hass_, call, *a, **k):
        seen.append(dict(call.data))

    with patch("custom_components.nova.services.async_analyze_camera", _record):
        await hass.services.async_call(DOMAIN, "analyze_camera", data, blocking=True)
    return seen


async def test_documented_analyze_camera_clip_fields_reach_the_handler(hass):
    await _setup(hass)
    seen = await _analyze_camera_calls(
        hass, {"entity_id": "camera.front", "frames": 3, "interval": 1.0})
    assert len(seen) == 1
    assert seen[0]["frames"] == 3 and seen[0]["interval"] == 1.0


async def test_analyze_camera_clip_fields_coerce_home_assistant_input(hass):
    await _setup(hass)
    seen = await _analyze_camera_calls(
        hass, {"entity_id": "camera.front", "frames": "2", "interval": "0.5"})
    assert seen[0]["frames"] == 2 and seen[0]["interval"] == 0.5


async def test_analyze_camera_omitted_clip_fields_keep_handler_defaults(hass):
    """No schema default is injected, so the handler's own fallback (1 frame,
    1.2 s) still applies."""
    await _setup(hass)
    seen = await _analyze_camera_calls(hass, {"entity_id": "camera.front"})
    assert len(seen) == 1
    assert "frames" not in seen[0] and "interval" not in seen[0]


@pytest.mark.parametrize("bad", [
    {"frames": 0}, {"frames": 7}, {"interval": 0.4}, {"interval": 5.1}, {"frames": "many"},
])
async def test_analyze_camera_out_of_range_clip_fields_are_rejected(hass, bad):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _analyze_camera_calls(hass, {"entity_id": "camera.front", **bad})


async def test_registered_websocket_commands_match_contract(hass):
    await _setup(hass)
    pinned = _fixture("websocket")
    commands = {t: v for t, v in hass.data["websocket_api"].items() if t.startswith("nova/")}
    assert set(commands) == set(pinned)
    for ctype, (handler, schema) in commands.items():
        keys = _schema_keys(schema)
        keys.pop("id", None)
        keys.pop("type", None)
        expected = {f: ("required" if v["required"] else "optional")
                    for f, v in pinned[ctype]["fields"].items()}
        assert keys == expected, f"{ctype} request fields changed"
        gated = handler.__code__.co_name == "with_admin"
        assert gated == pinned[ctype]["admin"], f"{ctype} admin gate changed"


@pytest.mark.parametrize("ctype", [
    "nova/get_calibration",
    "nova/get_cognitive_status",
    "nova/get_knowledge",
    "nova/list_automation_trials",
    "nova/get_spoken_history",
    "nova/list_decisions",
])
async def test_read_only_command_response_shape(hass, hass_ws_client, ctype):
    """Top-level keys of a real success response on a freshly set-up entry
    (observer off, empty stores). Values are never compared."""
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": ctype})
    resp = await client.receive_json()
    assert resp["success"], resp
    keys = sorted(resp["result"])
    path = _FIXTURES / "websocket_responses.json"
    if os.environ.get("NOVA_WRITE_CONTRACTS") == "1":
        data = json.loads(path.read_text()) if path.exists() else {}
        data[ctype] = keys
        path.write_text(json.dumps(dict(sorted(data.items())), indent=2, sort_keys=True) + "\n")
    pinned = _fixture("websocket_responses")[ctype]
    assert keys == pinned, (
        f"{ctype} response keys changed — removed: {sorted(set(pinned) - set(keys))}, "
        f"added: {sorted(set(keys) - set(pinned))}")


async def test_get_panel_data_top_level_sections(hass, hass_ws_client):
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/get_panel_data"})
    resp = await client.receive_json()
    assert resp["success"], resp
    assert set(resp["result"]) == set(_fixture("panel_boundary")["backend_top_level"])


async def test_conversation_entity_identity_survives_reload(hass):
    entry = await _setup(hass)
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)

    def _nova_entities():
        return [e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
                if e.domain == "conversation"]

    first = _nova_entities()
    assert len(first) == 1
    assert first[0].unique_id == entry.entry_id and first[0].platform == DOMAIN
    device = dev_reg.async_get(first[0].device_id)
    assert device.identifiers == {(DOMAIN, entry.entry_id)}
    assert (device.name, device.manufacturer, device.model) == ("Nova", "Nova", "Nova AI Assistant")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = _nova_entities()
    assert [e.entity_id for e in again] == [first[0].entity_id]
    assert again[0].unique_id == entry.entry_id
    assert len(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)) == 1


def test_stable_cross_module_imports():
    from custom_components.nova.agent import _verify_control, run_agent
    from custom_components.nova.websocket import (
        PANEL_WRITABLE_KEYS,
        async_register,
        invalidate_model_cache,
        nova_log,
        recent_conversation_log,
        recent_debug_log,
    )
    assert all(callable(f) for f in (_verify_control, run_agent, async_register,
                                     invalidate_model_cache, nova_log,
                                     recent_conversation_log, recent_debug_log))
    assert sorted(PANEL_WRITABLE_KEYS) == _fixture("config")["panel_writable_keys"]


def test_real_agent_tools_match_contract():
    from custom_components.nova import agent
    pinned = _fixture("agent_tools")
    names = sorted(t["function"]["name"] for t in agent.NOVA_TOOLS)
    assert names == sorted(pinned["tools"])
    assert sorted(agent.AGENT_PROFILES["homer"]["tools"]) == pinned["profiles"]["homer"]["tools"]
