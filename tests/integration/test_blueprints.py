"""Nova's two automation blueprints, run by a real Home Assistant.

Each blueprint is copied into the test config's blueprint folder, an
automation is built from it, and nova.analyze_on_event is replaced by a
recorder, so only the blueprint's own logic is under test."""

import asyncio
import contextlib
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

_SOURCE = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "blueprints"


async def _automation(hass, blueprint: str, inputs: dict):
    folder = Path(hass.config.path("blueprints", "automation", "nova_test"))
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(_SOURCE / blueprint, folder / blueprint)
    calls = async_mock_service(hass, "nova", "analyze_on_event")
    assert await async_setup_component(hass, "automation", {"automation": {
        "alias": "under test",
        "use_blueprint": {"path": f"nova_test/{blueprint}", "input": inputs},
    }})
    await _settle(hass)
    return calls


def _face(hass, camera="porch", name="Alex", unknown=False, confident=True):
    hass.bus.async_fire("nova_face_recognized", {
        "camera": camera, "camera_entity": f"camera.{camera}", "name": name,
        "confidence": 90.0 if confident else 20.0, "is_unknown": unknown,
        "is_confident": confident})


async def _settle(hass):
    """Let the automation run as far as it can. A run sitting in its
    cooldown never finishes on its own, so wait briefly instead of for
    everything to be done."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(hass.async_block_till_done(), 0.3)


async def _later(hass, seconds: float):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await _settle(hass)


@pytest.fixture(autouse=True)
async def _drain(hass):
    """Run every cooldown out at the end so teardown never waits on one."""
    yield
    await _later(hass, 4000)


# ── face recognition ────────────────────────────────────────────────────────

async def test_face_blueprint_announces_a_known_face(hass):
    calls = await _automation(hass, "nova_face_recognized.yaml", {})
    _face(hass, camera="front_door")
    await _settle(hass)
    assert [c.data for c in calls] == [{
        "entity_id": "camera.front_door",
        "reason": "Alex has been recognised at the front door"}]


async def test_face_blueprint_drops_repeats_inside_the_cooldown(hass):
    """Fixed: repeats used to queue behind the cooldown and be announced
    minutes late; now they are dropped."""
    calls = await _automation(hass, "nova_face_recognized.yaml", {"cooldown_seconds": 300})
    _face(hass)
    await _settle(hass)
    _face(hass, name="Sam")
    await _settle(hass)
    await _later(hass, 301)
    await _later(hass, 301)
    assert [c.data["reason"] for c in calls] == ["Alex has been recognised at the porch"]
    _face(hass, name="Sam")
    await _settle(hass)
    assert len(calls) == 2


async def test_face_blueprint_warns_about_an_unknown_face_without_a_confident_match(hass):
    """Fixed: unknown faces rarely pass the confidence check, which used to
    block every warning about them."""
    calls = await _automation(hass, "nova_face_recognized.yaml", {})
    _face(hass, name="unknown", unknown=True, confident=False)
    await _settle(hass)
    assert [c.data["reason"] for c in calls] == [
        "An unknown person has been seen at the porch; describe them"]


async def test_face_blueprint_never_names_an_unsure_match(hass):
    calls = await _automation(hass, "nova_face_recognized.yaml", {})
    _face(hass, confident=False)
    await _settle(hass)
    assert calls == []
    # Nothing was announced, so no cooldown holds the next one back.
    _face(hass, name="Sam")
    await _settle(hass)
    assert len(calls) == 1


@pytest.mark.parametrize("inputs,expected", [
    ({"greet_known": False}, ["An unknown person has been seen at the porch; describe them"]),
    ({"alert_unknown": False}, ["Alex has been recognised at the porch"]),
])
async def test_face_blueprint_switches(hass, inputs, expected):
    calls = await _automation(hass, "nova_face_recognized.yaml",
                              {"cooldown_seconds": 30, **inputs})
    _face(hass)
    await _settle(hass)
    await _later(hass, 31)
    _face(hass, name="unknown", unknown=True, confident=False)
    await _settle(hass)
    assert [c.data["reason"] for c in calls] == expected


async def test_face_blueprint_camera_filter(hass):
    calls = await _automation(hass, "nova_face_recognized.yaml",
                              {"target_camera": "camera.garden"})
    _face(hass, camera="porch")
    await _settle(hass)
    assert calls == []
    _face(hass, camera="garden")
    await _settle(hass)
    assert [c.data["entity_id"] for c in calls] == ["camera.garden"]


# ── doorbell or sensor ──────────────────────────────────────────────────────

async def test_doorbell_blueprint_counts_every_press_of_the_same_kind(hass):
    """Fixed: an event entity used to trigger only when its event type
    changed, so a second identical press was missed."""
    hass.states.async_set("event.front_doorbell", "unknown")
    calls = await _automation(hass, "nova_doorbell_analyze.yaml", {
        "trigger_entity": "event.front_doorbell", "camera_entity": "camera.door",
        "cooldown_seconds": 0})
    for stamp in ("2026-10-14T10:00:00+00:00", "2026-10-14T10:01:00+00:00"):
        hass.states.async_set("event.front_doorbell", stamp, {"event_type": "ring"})
        await _settle(hass)
        await _later(hass, 1)
    assert [c.data for c in calls] == [
        {"entity_id": "camera.door", "reason": "Someone has triggered the camera"}] * 2


async def test_doorbell_blueprint_ignores_an_event_entity_coming_back(hass):
    hass.states.async_set("event.front_doorbell", "unavailable")
    calls = await _automation(hass, "nova_doorbell_analyze.yaml", {
        "trigger_entity": "event.front_doorbell", "camera_entity": "camera.door"})
    hass.states.async_set("event.front_doorbell", "2026-10-14T10:00:00+00:00")
    await _settle(hass)
    assert calls == []


async def test_doorbell_blueprint_binary_sensor_only_on_turning_on(hass):
    hass.states.async_set("binary_sensor.porch_person", "off")
    calls = await _automation(hass, "nova_doorbell_analyze.yaml", {
        "trigger_entity": "binary_sensor.porch_person", "camera_entity": "camera.porch",
        "reason": "Person at the porch", "cooldown_seconds": 30})
    hass.states.async_set("binary_sensor.porch_person", "on")
    await _settle(hass)
    hass.states.async_set("binary_sensor.porch_person", "off")
    await _settle(hass)
    hass.states.async_set("binary_sensor.porch_person", "on")   # inside the cooldown
    await _settle(hass)
    await _later(hass, 31)
    assert [c.data for c in calls] == [
        {"entity_id": "camera.porch", "reason": "Person at the porch"}]
