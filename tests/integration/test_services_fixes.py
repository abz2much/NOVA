"""8.49.35 fixes to nova.* services, run against a real Home Assistant.

* lockdown takes only on/off and the older lockdown words; a typo or a call
  with no state is refused instead of lifting or engaging the lockdown,
* remember, forget and train_doorbell_backlog check their fields,
* observer_status and test_routing use the live panel settings, not only
  the setup entry,
* diagnose_doorbell finds automations made from Nova's doorbell blueprint
  in whatever folder it was imported into.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant.core import callback
from homeassistant.setup import async_setup_component

from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _no_real_config,
    _restore_nova_config,
    _setup,
)

_BLUEPRINTS = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "blueprints"


async def _call(hass, service, data=None):
    return await hass.services.async_call(DOMAIN, service, data or {}, blocking=True)


def _events(hass, event_type):
    seen = []

    @callback
    def _record(event):
        seen.append(dict(event.data))
    hass.bus.async_listen(event_type, _record)
    return seen


# ── lockdown ────────────────────────────────────────────────────────────────

@pytest.fixture
def lockdown_requests(monkeypatch):
    from custom_components.nova import cognitive_core
    seen = []

    async def _request(on, reason="", hass=None):
        seen.append(on)
        return True
    monkeypatch.setattr(cognitive_core, "request_lockdown", _request)
    return seen


@pytest.mark.parametrize("state, engaged", [
    ("on", True), ("engage", True), ("lock", True), (True, True), ("true", True),
    ("off", False), ("lift", False), ("unlock", False), (False, False), ("false", False),
])
async def test_lockdown_accepts_on_off_and_the_older_words(hass, lockdown_requests,
                                                           state, engaged):
    await _setup(hass)
    await _call(hass, "lockdown", {"state": state})
    assert lockdown_requests == [engaged]


@pytest.mark.parametrize("data", [{"state": "engaged"}, {"state": "maybe"}, {},
                                  {"enabled": "on"}])
async def test_lockdown_refuses_anything_else(hass, lockdown_requests, data):
    """Fixed: an unknown word (a typo such as "engaged") used to lift the
    lockdown, and a call without a state used to engage it."""
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call(hass, "lockdown", data)
    assert lockdown_requests == []


# ── knowledge and backlog input checks ──────────────────────────────────────

@pytest.fixture
def knowledge_calls(monkeypatch):
    from custom_components.nova import knowledge
    seen = []
    monkeypatch.setattr(knowledge, "remember",
                        lambda *a, **k: seen.append(("remember", a, k)) or {"id": 1})
    monkeypatch.setattr(knowledge, "forget",
                        lambda **k: seen.append(("forget", k)) or 1)
    return seen


@pytest.mark.parametrize("service, data", [
    ("remember", {"key": "bin day"}),
    ("remember", {"key": "bin day", "value": "   "}),
    ("remember", {"key": "bin day", "value": "Tuesday", "kind": "rumour"}),
    ("remember", {"key": "bin day", "value": "Tuesday", "ttl_seconds": "soon"}),
    ("remember", {"key": "bin day", "value": "Tuesday", "ttl_seconds": -5}),
    ("forget", {"id": "abc"}),
    ("forget", {"id": -1}),
    ("train_doorbell_backlog", {"limit": 0}),
    ("train_doorbell_backlog", {"limit": 501}),
])
async def test_bad_input_is_an_error_not_a_silent_no_op(hass, knowledge_calls, service, data):
    """Fixed: these services had no schema, so a bad call did nothing and
    reported success."""
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call(hass, service, data)
    assert knowledge_calls == []


async def test_good_knowledge_calls_still_store_and_forget(hass, knowledge_calls):
    await _setup(hass)
    await _call(hass, "remember", {"key": " bin day ", "value": "Tuesday",
                                   "kind": "preference", "ttl_seconds": "60"})
    await _call(hass, "forget", {"id": "7"})
    await _call(hass, "forget", {"key": "bin day", "subject": "household"})
    kind, args, kwargs = knowledge_calls[0]
    assert (kind, args) == ("remember", ("bin day", "Tuesday"))
    assert kwargs["kind"] == "preference" and kwargs["ttl_seconds"] == 60.0
    assert knowledge_calls[1:] == [
        ("forget", {"fact_id": 7, "subject": None, "key": None}),
        ("forget", {"fact_id": None, "subject": "household", "key": "bin day"})]


async def test_backlog_limit_inside_the_range_reaches_the_scan(hass, monkeypatch):
    from custom_components.nova import doorbell_training
    limits = []

    async def _scan(hass_, analyze, honorific, limit=40):
        limits.append(limit)
        return {"ok": False, "reason": "nothing recorded"}
    monkeypatch.setattr(doorbell_training, "scan_backlog", _scan)
    await _setup(hass)
    await _call(hass, "train_doorbell_backlog", {"limit": "500"})
    await _call(hass, "train_doorbell_backlog")
    assert limits == [500, 40]


# ── live panel settings ─────────────────────────────────────────────────────

async def test_observer_status_reads_the_panel_settings(hass, monkeypatch):
    """Fixed: bedrooms and quiet hours came from the setup entry only, so a
    panel configured install reported the defaults."""
    from custom_components.nova import sleep_detection
    asked = []

    def _is_sleeping(hass_, **kwargs):
        asked.append(kwargs)
        return False, "awake"
    monkeypatch.setattr(sleep_detection, "is_sleeping", _is_sleeping)
    entry = await _setup(hass)
    entry.runtime_data.runtime_config.update({
        "bedroom_areas": ["loft"], "observer_quiet_start": "23:30",
        "observer_quiet_end": "06:15"})
    events = _events(hass, "nova_observer_status")
    await _call(hass, "observer_status")
    await hass.async_block_till_done()
    assert asked == [{"bedroom_area_ids": ["loft"], "quiet_start": "23:30",
                      "quiet_end": "06:15"}]
    assert events[0]["bedroom_areas"] == ["loft"]


async def test_routing_diagnostic_reads_the_panel_settings(hass, caplog):
    """Fixed: the broadcast group and TTS engine came from the setup entry
    only."""
    entry = await _setup(hass)
    entry.runtime_data.runtime_config.update({
        "broadcast_group": "media_player.downstairs",
        "announcement_speakers": ["media_player.hall"],
        "satellite_pairings": '{"assist_satellite.hall": "media_player.hall"}'})
    with caplog.at_level(logging.WARNING, logger="custom_components.nova.services"):
        await _call(hass, "test_routing")
    text = caplog.text
    assert "Broadcast group (config): 'media_player.downstairs'" in text
    assert "Announcement speakers (panel): ['media_player.hall']" in text
    assert "Satellite pairings (panel): {'assist_satellite.hall': 'media_player.hall'}" in text


# ── doorbell diagnostic ─────────────────────────────────────────────────────

async def _doorbell_automation(hass, folder: str, alias: str = "doorbell look"):
    target = Path(hass.config.path("blueprints", "automation", folder))
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(_BLUEPRINTS / "nova_doorbell_analyze.yaml", target)
    hass.states.async_set("event.front_doorbell", "unknown")
    assert await async_setup_component(hass, "automation", {"automation": {
        "alias": alias,
        "use_blueprint": {"path": f"{folder}/nova_doorbell_analyze.yaml", "input": {
            "trigger_entity": "event.front_doorbell", "camera_entity": "camera.door"}},
    }})
    await hass.async_block_till_done()


async def test_doorbell_diagnostic_finds_the_blueprint_automation(hass):
    """Fixed: the check looked for one fixed automation name, so it failed on
    every install that didn't use that exact name."""
    await _setup(hass)
    await _doorbell_automation(hass, "anyone")
    events = _events(hass, "nova_doorbell_diag")
    await _call(hass, "diagnose_doorbell")
    await hass.async_block_till_done()
    checks = {c["check"]: c for c in events[0]["checks"]}
    assert checks["automation exists"]["ok"] is True
    assert checks["automation exists"]["entity_id"] == "automation.doorbell_look"
    assert checks["automation enabled"]["ok"] is True
    assert checks["analyze_on_event service"]["ok"] is True


async def test_doorbell_diagnostic_reports_a_disabled_automation(hass):
    await _setup(hass)
    await _doorbell_automation(hass, "elsewhere")
    await hass.services.async_call("automation", "turn_off",
                                   {"entity_id": "automation.doorbell_look"}, blocking=True)
    events = _events(hass, "nova_doorbell_diag")
    await _call(hass, "diagnose_doorbell")
    await hass.async_block_till_done()
    assert events[0]["verdict"] == "Automation exists but is disabled"


async def test_doorbell_diagnostic_without_the_blueprint(hass):
    await _setup(hass)
    events = _events(hass, "nova_doorbell_diag")
    await _call(hass, "diagnose_doorbell")
    await hass.async_block_till_done()
    assert events[0]["checks"][0]["ok"] is False
    assert events[0]["verdict"].startswith("Automation missing")
