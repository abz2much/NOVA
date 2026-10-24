"""Nova's state entities (8.70.0) against a real Home Assistant (PHACC).

Setup adds the sensors and binary sensors on the Nova device, they follow
a timed shush and a nap without polling, the suggestion services refuse a
user who is not an admin, and unload leaves them unavailable.
"""
from datetime import timedelta

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .test_runtime_data import _no_real_config, _restore_nova_config, _setup  # noqa: F401
from .test_wiring_smoke import DOMAIN

KEYS = {"sensor": ("status", "nap_ends", "last_announcement", "next_reminder", "briefing",
                   "camera_summary", "energy_note", "suggestions"),
        "binary_sensor": ("quiet", "lockdown")}


@pytest.fixture(autouse=True)
def _clean_gate(tmp_path, monkeypatch):
    from custom_components.nova import output_gate, sleep_detection
    monkeypatch.setattr(output_gate, "_mutes_path", lambda: str(tmp_path / "mutes.json"))
    output_gate.unshush()
    sleep_detection.clear_nap()
    yield
    output_gate.unshush()
    sleep_detection.clear_nap()


def _ids(hass, entry):
    reg = er.async_get(hass)
    return {key: reg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}-{key}")
            for platform, keys in KEYS.items() for key in keys}


async def test_entities_follow_shush_and_nap(hass, freezer):
    entry = await _setup(hass)
    ids = _ids(hass, entry)
    assert all(ids.values()), ids
    assert hass.states.get(ids["status"]).state == "observer_off"
    assert hass.states.get(ids["quiet"]).state == "off"
    assert hass.states.get(ids["lockdown"]).state == "off"
    assert hass.states.get(ids["next_reminder"]).state == "0"

    await hass.services.async_call(DOMAIN, "shush", {"all": True, "minutes": 1},
                                   blocking=True)
    await hass.async_block_till_done()
    quiet = hass.states.get(ids["quiet"])
    assert quiet.state == "on" and quiet.attributes["mute_all"] is True
    assert quiet.attributes["until"] is not None
    assert hass.states.get(ids["status"]).state == "quiet"

    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(ids["quiet"]).state == "off"

    await hass.services.async_call(DOMAIN, "nap", {"duration_minutes": 5}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ids["status"]).state == "napping"
    assert hass.states.get(ids["nap_ends"]).state not in ("unknown", "unavailable")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ids["status"]).state == "unavailable"


async def test_suggestion_services_need_an_admin(hass, hass_read_only_user):
    await _setup(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "suggestion_dismiss", {"suggestion_id": 1}, blocking=True,
            context=Context(user_id=hass_read_only_user.id))
