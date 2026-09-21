"""Eufy native-sensor dispatch feeding semantic learning (Phase 4, v7.109.0)
— against a real Home Assistant instance (PHACC), since eufy.py's role
discovery needs real entity/device registry relationships and _auto_eufy is
a closure inside async_setup_entry that can't be unit-tested directly.

Verifies: actual Eufy unique-ID/role mappings dispatch to
camera_semantic.record_event with the right label/source/attribution;
package roles still go through package_monitor unchanged; no vision-LLM
call for routine native detections; a sensor already "on" at startup does
not fire as a new detection; existing announcement/notification behaviour
is unaffected (additive only).
"""
import pytest
from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry
from .test_wiring_smoke import _make_entry

DOMAIN = "nova"


async def _setup_eufy_camera(hass, config_entry_id: str, *, device_id_suffix: str = "1") -> dict:
    """Register a Eufy device with a camera + every recognised sibling
    sensor eufy.py's _ROLE_SUFFIXES maps, using the EXACT unique_id shape
    Nova's own eufy.py docstring documents:
    eufy_security_<serial>_device_<role>. Returns {role: entity_id}."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    serial = f"T8210{device_id_suffix}"

    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry_id,
        identifiers={("eufy_security", serial)},
        name=f"Eufy Doorbell {device_id_suffix}",
    )

    camera = ent_reg.async_get_or_create(
        "camera", "eufy_security", f"eufy_security_{serial}_device_camera",
        config_entry=None, device_id=device.id,
    )
    hass.states.async_set(camera.entity_id, "idle")

    roles = {
        "ringing": "ringing",
        "stranger": "strangerPersonDetected",
        "person": "personDetected",
        "vehicle": "motionDetectionTypeVehicle",
        "pet": "petDetected",
        "package_delivered": "packageDelivered",
        "package_stranded": "packageStranded",
        "package_taken": "packageTaken",
    }
    entities = {}
    for role, suffix in roles.items():
        domain = "switch" if role == "vehicle" else "binary_sensor"
        entry = ent_reg.async_get_or_create(
            domain, "eufy_security", f"eufy_security_{serial}_device_{suffix}",
            config_entry=None, device_id=device.id,
        )
        hass.states.async_set(entry.entity_id, "off")
        entities[role] = entry.entity_id
    await hass.async_block_till_done()
    entities["camera"] = camera.entity_id
    return entities


async def _setup_nova_with_eufy(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    # Eufy discovery runs once at setup, off whatever's already registered —
    # register the device/entities BEFORE async_setup_entry so _auto_eufy's
    # camera->role map picks them up (matches the real-world order: the
    # eufy_security integration is already set up before Nova starts).
    entities = await _setup_eufy_camera(hass, entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entities


@pytest.fixture(autouse=True)
def _reset_semantic_dedup():
    """camera_semantic's dedup cache is a module-level dict — reset between
    tests in this file for the same reason test_websocket_security.py resets
    the model-discovery cache (Phase 3 precedent)."""
    from custom_components.nova import camera_semantic
    camera_semantic.reset_dedup_state()
    yield
    camera_semantic.reset_dedup_state()


async def test_person_detection_records_semantic_event(hass, monkeypatch):
    from custom_components.nova import camera_semantic
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["person"], "on")
    await hass.async_block_till_done()

    matches = [c for c in calls if c.get("label") == "person" and c.get("source") == "eufy"]
    assert matches, f"expected a person semantic event, got {calls}"
    assert matches[0]["camera_entity"] == ent["camera"]
    assert matches[0].get("attribute", True) is True  # known/regular face -> attribution attempted


async def test_stranger_detection_records_unattributed_person_event(hass, monkeypatch):
    from custom_components.nova import camera_semantic
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["stranger"], "on")
    await hass.async_block_till_done()

    matches = [c for c in calls if c.get("source") == "eufy" and c.get("camera_entity") == ent["camera"]
               and c.get("attribute") is False]
    assert matches, f"expected an unattributed stranger event, got {calls}"
    assert matches[0]["label"] == "person"


async def test_vehicle_detection_records_semantic_event(hass, monkeypatch):
    from custom_components.nova import camera_semantic
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["vehicle"], "on")
    await hass.async_block_till_done()

    matches = [c for c in calls if c.get("label") == "vehicle" and c.get("source") == "eufy"]
    assert matches, f"expected a vehicle semantic event, got {calls}"


async def test_pet_detection_records_animal_semantic_event(hass, monkeypatch):
    """Eufy's pet role has NO announcement/action today — Phase 4 adds
    ONLY semantic recording for it, nothing spoken."""
    from custom_components.nova import camera_semantic
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["pet"], "on")
    await hass.async_block_till_done()

    matches = [c for c in calls if c.get("label") == "animal" and c.get("source") == "eufy"]
    assert matches, f"expected an animal semantic event, got {calls}"


async def test_package_delivered_still_goes_through_package_monitor_state_machine(hass, monkeypatch):
    """Package roles must continue using the EXISTING evaluate() state
    machine — Phase 4 does not bypass or replace it."""
    from custom_components.nova import package_monitor as pm
    calls = []

    async def _fake_evaluate(hass_, client, honorific, tts, spk, entity_id, det, source="periodic"):
        calls.append((entity_id, det, source))
        return False
    monkeypatch.setattr(pm, "evaluate", _fake_evaluate)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["package_delivered"], "on")
    await hass.async_block_till_done()

    assert calls, "package_delivered must still reach package_monitor.evaluate()"
    assert calls[0][0] == ent["camera"]
    assert calls[0][1]["package"] is True
    assert calls[0][2] == "eufy"


async def test_package_states_feed_semantic_learning_via_existing_log_hook(hass, monkeypatch):
    """package_monitor._log() (the existing single choke point for every
    real package transition) is the hook — not a second, parallel path."""
    from custom_components.nova import camera_semantic
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["package_delivered"], "on")
    await hass.async_block_till_done()

    matches = [c for c in calls if c.get("label") == "package" and c.get("package_state") == "delivered"]
    assert matches, f"expected a package/delivered semantic event, got {calls}"


async def test_startup_active_sensor_does_not_fire_as_new_detection(hass, monkeypatch):
    """A sensor already 'on' when Nova starts must never be recorded as a
    NEW event — async_track_state_change_event only fires on a transition
    observed AFTER registration, never for pre-existing state."""
    from custom_components.nova import camera_semantic
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    ent = await _setup_eufy_camera(hass, entry.entry_id)
    # The person sensor is ALREADY "on" before Nova's listener registers.
    hass.states.async_set(ent["person"], "on")
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert calls == [], f"a pre-existing active state must not fire as a new detection, got {calls}"


async def test_semantic_recording_does_not_duplicate_existing_announcement(hass, monkeypatch):
    """Additive means additive: the pre-existing vehicle announcement fires
    exactly once, and semantic recording happens ALONGSIDE it — neither
    suppresses nor doubles the other."""
    from custom_components.nova import camera_semantic
    announced = []
    recorded = []

    async def _fake_announce(hass_, msg, *a, **k):
        announced.append(msg)
    monkeypatch.setattr("custom_components.nova.tts_helper.async_announce", _fake_announce)

    async def _fake_record(hass_, **kw):
        recorded.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["vehicle"], "on")
    await hass.async_block_till_done()

    assert len(announced) == 1, f"vehicle announcement must fire exactly once, got {announced}"
    assert len([c for c in recorded if c.get("label") == "vehicle"]) == 1


async def test_frigate_bus_event_feeds_semantic_learning(hass, monkeypatch):
    """Frigate's frigate_event -> Nova's own nova_camera_event -> semantic
    learning, without re-parsing Frigate's raw payload a second time.

    Calls _handle_semantic_camera_event directly with a real HA Event
    object, rather than round-tripping through hass.bus.async_fire twice
    (frigate_event, then the nova_camera_event it triggers): PHACC's frame-
    safety checker flags the SECOND fire — from inside a lambda-wrapped
    @callback, the exact pre-existing pattern register_event_listeners
    already uses for nest_event/frigate_event, unchanged by Phase 4 — as
    off-thread and the resulting async_create_task doesn't resolve inside
    this test's event-loop turn. This still exercises the real function
    against the real hass instance and registries; only the outer bus-fire
    hop (HA's own, pre-existing) is bypassed. _map_bus_label's mapping
    logic is covered directly in test_camera_semantic_source_mapping.py."""
    from custom_components.nova import camera, camera_semantic
    from homeassistant.core import Event
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, _ = await _setup_nova_with_eufy(hass)
    hass.states.async_set("camera.frigate_porch", "idle")
    await hass.async_block_till_done()

    event = Event("nova_camera_event", {
        "entity_id": "camera.frigate_porch", "source": "frigate",
        "label": "person", "confidence": 91,
    })
    camera._handle_semantic_camera_event(hass, event)
    await hass.async_block_till_done()

    matches = [c for c in calls if c.get("source") == "frigate" and c.get("label") == "person"]
    assert matches, f"expected a Frigate-sourced person semantic event, got {calls}"
    assert matches[0]["confidence"] == 91  # Frigate's own score, normalised 0-100, not invented


async def test_nest_bus_event_feeds_semantic_learning_with_no_confidence(hass, monkeypatch):
    """Nest exposes no numeric confidence — must be accepted, not rejected,
    and never have a score invented for it. See the docstring above for why
    this calls the handler directly rather than through hass.bus.async_fire."""
    from custom_components.nova import camera, camera_semantic
    from homeassistant.core import Event
    calls = []

    async def _fake_record(hass_, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(camera_semantic, "record_event", _fake_record)

    entry, _ = await _setup_nova_with_eufy(hass)
    hass.states.async_set("camera.nest_doorbell", "idle")
    await hass.async_block_till_done()

    event = Event("nova_camera_event", {
        "entity_id": "camera.nest_doorbell", "source": "nest",
        "label": "camera_person", "confidence": None,
    })
    camera._handle_semantic_camera_event(hass, event)
    await hass.async_block_till_done()

    matches = [c for c in calls if c.get("source") == "nest" and c.get("label") == "person"]
    assert matches, f"expected a Nest-sourced person semantic event, got {calls}"
    assert matches[0]["confidence"] is None


async def test_no_vision_llm_call_for_routine_person_detection(hass, monkeypatch):
    """The whole point of Eufy's native sensors: person/vehicle/pet/known-
    visitor roles must never trigger a vision-LLM call."""
    vision_called = {"count": 0}

    async def _fake_analyze(*a, **k):
        vision_called["count"] += 1
        return {"success": True}
    monkeypatch.setattr(
        "custom_components.nova.camera.async_analyze_camera", _fake_analyze)

    entry, ent = await _setup_nova_with_eufy(hass)
    hass.states.async_set(ent["person"], "on")
    await hass.async_block_till_done()
    hass.states.async_set(ent["vehicle"], "on")
    await hass.async_block_till_done()
    hass.states.async_set(ent["pet"], "on")
    await hass.async_block_till_done()

    assert vision_called["count"] == 0
