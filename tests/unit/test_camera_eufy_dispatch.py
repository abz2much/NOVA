"""Tests for camera.py's Eufy integration: _camera_integration() recognising
eufy_security, and _fetch_event_media_image() dispatching to the right
backend's 'frame frozen at the moment of the event' fetch — Nest's recorded
event media or Eufy's image.*_event_image entity."""
import sys
import types

import pytest


@pytest.fixture
def cam(load):
    return load("camera")


def _stub_registry(monkeypatch, entries: dict):
    """entries: entity_id -> platform. Matches conftest's globally-installed
    homeassistant.helpers.entity_registry stub shape."""
    er = sys.modules["homeassistant.helpers.entity_registry"]

    def _async_get(entity_id):
        platform = entries.get(entity_id)
        return types.SimpleNamespace(platform=platform) if platform else None

    monkeypatch.setattr(er, "async_get",
                        lambda hass: types.SimpleNamespace(
                            async_get=_async_get, entities={}))


def test_camera_integration_recognises_eufy(cam, fake_hass, monkeypatch):
    _stub_registry(monkeypatch, {"camera.front_door_bell": "eufy_security"})
    assert cam._camera_integration(fake_hass, "camera.front_door_bell") == "eufy"


def test_camera_integration_still_recognises_nest_and_frigate(cam, fake_hass, monkeypatch):
    _stub_registry(monkeypatch, {
        "camera.nest_cam": "nest",
        "camera.frigate_cam": "frigate",
        "camera.generic": "some_other_platform",
    })
    assert cam._camera_integration(fake_hass, "camera.nest_cam") == "nest"
    assert cam._camera_integration(fake_hass, "camera.frigate_cam") == "frigate"
    assert cam._camera_integration(fake_hass, "camera.generic") == "other"


async def test_fetch_event_media_dispatches_to_eufy(cam, load, fake_hass, monkeypatch):
    eufy_mod = load("eufy")
    monkeypatch.setattr(cam, "_camera_integration", lambda h, e: "eufy")
    monkeypatch.setattr(eufy_mod, "discover_roles",
                        lambda h, e: {"event_image": "image.front_door_bell_event_image"})

    fetched = {}
    async def _fake_fetch(hass, image_entity_id):
        fetched["entity"] = image_entity_id
        return b"jpeg-bytes"
    monkeypatch.setattr(eufy_mod, "async_fetch_event_image", _fake_fetch)

    result = await cam._fetch_event_media_image(fake_hass, "camera.front_door_bell")
    assert result == b"jpeg-bytes"
    assert fetched["entity"] == "image.front_door_bell_event_image"


async def test_fetch_event_media_eufy_with_no_image_role_returns_none(cam, load, fake_hass, monkeypatch):
    eufy_mod = load("eufy")
    monkeypatch.setattr(cam, "_camera_integration", lambda h, e: "eufy")
    monkeypatch.setattr(eufy_mod, "discover_roles", lambda h, e: {})
    result = await cam._fetch_event_media_image(fake_hass, "camera.front_door_bell")
    assert result is None


async def test_fetch_event_media_dispatches_to_nest_unchanged(cam, fake_hass, monkeypatch):
    monkeypatch.setattr(cam, "_camera_integration", lambda h, e: "nest")
    called = {}
    async def _fake_nest_fetch(hass, entity_id):
        called["entity"] = entity_id
        return b"nest-bytes"
    monkeypatch.setattr(cam, "_fetch_nest_event_image", _fake_nest_fetch)

    result = await cam._fetch_event_media_image(fake_hass, "camera.nest_cam")
    assert result == b"nest-bytes"
    assert called["entity"] == "camera.nest_cam"


async def test_fetch_event_media_no_fallback_for_other_platforms(cam, fake_hass, monkeypatch):
    monkeypatch.setattr(cam, "_camera_integration", lambda h, e: "frigate")
    result = await cam._fetch_event_media_image(fake_hass, "camera.frigate_cam")
    assert result is None
