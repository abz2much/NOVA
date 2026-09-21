"""Host health awareness (Phase 10) PHACC coverage — driven against a REAL
Home Assistant instance (real entity registry, real service registration,
real hass.states), not the hand-rolled fakes used by tests/unit/.

Companions to the unit-level proofs:
  tests/unit/test_host_health.py (discovery/mapping/reading/evaluator)
  tests/unit/test_host_health_alerts.py (dispatch gating)
  tests/unit/test_host_health_diagnostics_homer.py (system_diagnostics/HOMER)
  tests/unit/test_host_health_config.py (safety_config validation)

Here the same technique test_websocket_security.py already uses drives real
entity_registry.async_get_or_create() (proving discovery works against a
genuine registry, not a fake), a real registered light.turn_on service
(proving an over-threshold reading never actuates anything), and the real
admin-gated nova/update_config command for the new host_health_* keys.
"""
import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry
from .test_wiring_smoke import _make_entry

DOMAIN = "nova"


async def _setup_nova(hass) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _register_sysmon_sensor(hass, entity_id, sysmon_key, *, disabled=False):
    registry = er.async_get(hass)
    unique_id = f"{sysmon_key}_"
    entry = registry.async_get_or_create(
        "sensor", "systemmonitor", unique_id,
        suggested_object_id=entity_id.split(".", 1)[1],
        translation_key=sysmon_key,
    )
    if disabled:
        registry.async_update_entity(
            entry.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    return entry.entity_id


@pytest.fixture(autouse=True)
def _reset_host_health_state():
    from custom_components.nova import host_health
    host_health.reset_state()
    yield
    host_health.reset_state()


# ── discovery against a REAL entity registry ────────────────────────────────

async def test_discovers_real_registry_entities_by_platform_ownership(hass):
    entity_id = _register_sysmon_sensor(hass, "sensor.processor_use", "processor_use")
    hass.states.async_set(entity_id, "17", {"unit_of_measurement": "%", "state_class": "measurement"})

    from custom_components.nova import host_health
    candidates = host_health.discover_candidates(hass)
    assert [c.entity_id for c in candidates["cpu_percent"]] == [entity_id]


async def test_disabled_real_entity_reported_not_auto_enabled(hass):
    entity_id = _register_sysmon_sensor(
        hass, "sensor.processor_temperature", "processor_temperature", disabled=True)

    from custom_components.nova import host_health
    mappings = host_health.resolve_mappings(hass, {})
    assert mappings["cpu_temperature"].status == "disabled"
    assert mappings["cpu_temperature"].entity_id is None
    # Confirm against the real registry: still disabled, Nova never touched it.
    reg_entry = er.async_get(hass).async_get(entity_id)
    assert reg_entry.disabled_by is not None


# ── real dispatch: an over-threshold reading never actuates anything ───────

async def test_persistent_problem_never_calls_a_real_service(hass, monkeypatch):
    entity_id = _register_sysmon_sensor(hass, "sensor.processor_use", "processor_use")
    hass.states.async_set(entity_id, "95", {"unit_of_measurement": "%", "state_class": "measurement"})
    hass.states.async_set("light.hallway", "off")

    calls = []
    async def fake_turn_on(call):
        calls.append(call)
    hass.services.async_register("light", "turn_on", fake_turn_on)

    from custom_components.nova import host_health
    box = {"t": 0.0}
    monkeypatch.setattr(host_health, "_now", lambda: box["t"])
    dispatched = []
    async def _fake_dispatch(hass_, config_, message, category):
        dispatched.append((category, message))
    monkeypatch.setattr(host_health, "_dispatch_alert", _fake_dispatch)

    config = {"host_health_enabled": True, "host_health_alerts_enabled": True,
             "host_health_persistence_minutes": 10}
    for _ in range(6):
        box["t"] += 120
        await host_health.tick(hass, config)

    assert calls == []  # the real light service never fired
    assert any(c == "host_health_problem" for c, _ in dispatched)


# ── nova/update_config admin gate for the new keys ──────────────────────────

async def test_update_config_host_health_enabled_requires_admin(
    hass, hass_ws_client, hass_read_only_access_token,
):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    await client.send_json_auto_id({
        "type": "nova/update_config", "key": "host_health_enabled", "value": True,
    })
    resp = await client.receive_json()
    assert resp["success"] is False


async def test_update_config_host_health_enabled_admin_succeeds(hass, hass_ws_client):
    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "nova/update_config", "key": "host_health_enabled", "value": True,
    })
    resp = await client.receive_json()
    assert resp["success"] is True


async def test_update_config_host_health_mappings_rejects_bad_json_as_admin(hass, hass_ws_client):
    """Even an admin's write is rejected if the value fails safety_config's
    validation — the value has to be a JSON-encoded string of sensor.*
    entities, not an arbitrary string or a native object."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "nova/update_config", "key": "host_health_mappings",
        "value": '{"cpu_percent": "light.not_a_sensor"}',
    })
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "invalid_value"


async def test_update_config_host_health_mappings_accepts_valid_json_string(hass, hass_ws_client):
    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "nova/update_config", "key": "host_health_mappings",
        "value": '{"cpu_percent": "sensor.processor_use"}',
    })
    resp = await client.receive_json()
    assert resp["success"] is True


# ── get_panel_data surfaces live discovery/mapping/snapshot ────────────────

async def test_get_panel_data_includes_host_health_status(hass, hass_ws_client):
    entity_id = _register_sysmon_sensor(hass, "sensor.processor_use", "processor_use")
    hass.states.async_set(entity_id, "12", {"unit_of_measurement": "%", "state_class": "measurement"})
    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/get_panel_data"})
    resp = await client.receive_json()
    assert resp["success"] is True
    status = resp["result"]["config"]["host_health_status"]
    assert "error" not in status
    keys = {m["key"] for m in status["metrics"]}
    assert "cpu_percent" in keys
    cpu = next(m for m in status["metrics"] if m["key"] == "cpu_percent")
    assert cpu["status"] == "mapped"
    assert cpu["entity_id"] == entity_id


# ── the periodic sweep actually registers during real setup ────────────────

async def test_host_health_scheduler_task_registers_on_setup(hass):
    entry = await _setup_nova(hass)
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    sched = data.get("scheduler")
    assert sched is not None
    task_names = {t.get("name") for t in sched.status()}
    assert "host_health" in task_names
