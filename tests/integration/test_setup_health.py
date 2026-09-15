"""Setup Doctor (Phase 2) — the one check tests/unit/'s fakes cannot credibly
prove: real Assist pipeline introspection. assist_pipeline auto-creates a
default pipeline the first time its storage loads (see
homeassistant.components.assist_pipeline.pipeline.PipelineStorageCollection.
_async_load_data), so a real hass genuinely exercises the
async_get_pipelines / async_update_pipeline path setup_health.py calls —
something a fake assist_pipeline module can only simulate, not prove.
"""
from homeassistant.components import assist_pipeline
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry


async def _setup_nova(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_get_setup_health_rejects_non_admin(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id({"type": "nova/get_setup_health"})
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_assist_pipeline_check_ok_against_real_pipeline(hass, hass_ws_client):
    """A real Assist pipeline, renamed to Nova and pointed at Nova's actual
    conversation entity, must report ok — not a fake standing in for one."""
    from custom_components.nova import bootstrap

    await _setup_nova(hass)
    assert await async_setup_component(hass, "assist_pipeline", {})

    agent = bootstrap._find_nova_agent(hass)
    assert agent, "Nova's conversation entity did not register"

    pipelines = list(assist_pipeline.async_get_pipelines(hass))
    assert pipelines, "assist_pipeline did not auto-create a default pipeline"
    await assist_pipeline.async_update_pipeline(
        hass, pipelines[0], name="Nova", conversation_engine=agent)

    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "nova/get_setup_health"})
    resp = await ws.receive_json()

    assert resp["success"] is True
    checks = {c["key"]: c for c in resp["result"]["checks"]}
    assert checks["assist_pipeline"]["status"] == "ok"


async def test_assist_pipeline_check_warns_on_wrong_conversation_engine(hass, hass_ws_client):
    """A real pipeline named Nova but left on its default (non-Nova)
    conversation engine must be flagged, not silently reported healthy."""
    await _setup_nova(hass)
    assert await async_setup_component(hass, "assist_pipeline", {})

    pipelines = list(assist_pipeline.async_get_pipelines(hass))
    assert pipelines
    await assist_pipeline.async_update_pipeline(hass, pipelines[0], name="Nova")

    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "nova/get_setup_health"})
    resp = await ws.receive_json()

    checks = {c["key"]: c for c in resp["result"]["checks"]}
    assert checks["assist_pipeline"]["status"] == "warn"
    assert "suggested_fix" in checks["assist_pipeline"]


async def test_setup_health_does_not_duplicate_service_health_checks(hass, hass_ws_client):
    """The 8 core-service checks must appear exactly once each — proving the
    WS response really is service_health's own output, not a re-derived
    duplicate set living alongside it."""
    await _setup_nova(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "nova/get_setup_health"})
    resp = await ws.receive_json()

    keys = [c["key"] for c in resp["result"]["checks"]]
    core = {"llm", "embeddings", "tts", "stt", "cameras", "routines", "database", "scheduler"}
    for key in core:
        assert keys.count(key) == 1, f"{key} appeared {keys.count(key)} times"
