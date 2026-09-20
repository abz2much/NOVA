"""Behavioural tests for the Action Audit Log paths added in the v3
reconciliation pass: sentinel/appliance/sleep-prompt/scene notifications,
lockdown engage+breach, intrusion/cognitive alert dispatch, accepted
proactive offers, and local_engine's bulk/goodnight/scene fast paths.

Each test proves the same three things the original design requires:
one request_id per logical action, the right row COUNT (1 for a single
call, N for a batch), and a correct terminal execution_result.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

import pytest


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture
def isolated_db(al, monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    monkeypatch.setattr(al, "_DEFAULT_DB", path)
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass


def _stub_event_helpers(monkeypatch):
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    ev.async_call_later = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)


# ── sentinel.py: proactive alert (voice + phone push share one request) ─────

async def test_sentinel_rule_phone_push_is_logged_and_linked_to_voice(
    load, fake_hass, monkeypatch, isolated_db, al,
):
    _stub_event_helpers(monkeypatch)
    sentinel_mod = load("sentinel")

    monkeypatch.setattr(sentinel_mod, "save_sentinel_event", lambda *a, **k: None)
    monkeypatch.setattr(sentinel_mod, "save_message", lambda *a, **k: None)

    announced = []
    async def fake_announce(hass, text, tts, speakers, context="", action_request_id=None):
        announced.append((text, action_request_id))
    monkeypatch.setattr(sentinel_mod, "async_announce", fake_announce)
    monkeypatch.setattr(
        sentinel_mod.nova_config, "runtime_get",
        lambda hass, entry, key, default=None:
            "notify.mobile_app_test" if key == "notify_service" else default,
    )
    fake_hass.services.register("notify", "mobile_app_test")

    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                          friendly_name="Front Door")
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                   rules=[], entry=None)
    rule = {"id": "door_left_open",
            "message": "{honorific}, {friendly_name} has been open for {minutes} minutes."}

    await s._announce_rule("binary_sensor.front_door", rule, 10)

    assert len(announced) == 1
    _text, req_id = announced[0]
    assert req_id

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["request_id"] == req_id
    assert row["action"] == "notify"
    assert row["targets"][0]["execution_result"] == "accepted"


# ── appliance_monitor.py: notify_only phone push ─────────────────────────────

async def test_appliance_notify_only_push_is_logged(
    load, fake_hass, monkeypatch, isolated_db, al,
):
    am = load("appliance_monitor")
    og = load("output_gate")
    monkeypatch.setattr(og, "can_announce", lambda **kw: (True, ""))
    monkeypatch.setattr(og, "record_announcement", lambda **kw: None)
    audio_routing = load("audio_routing")
    monkeypatch.setattr(audio_routing, "observer_speak_target",
                         lambda *a, **k: ([], "notify_only"))
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "is_sleeping", lambda *a, **k: (False, None))

    am._MON.hass = fake_hass
    am._MON.config = {"notify_service": "notify.mobile_app_test"}
    fake_hass.services.register("notify", "mobile_app_test")

    sensor = am._SensorState(
        entity_id="sensor.dryer_status", friendly_name="Dryer",
        appliance=am.ApplianceType.DRYER, discovery_method="native_status",
        peak_power=500.0,
    )
    await am._announce_done(sensor, "dryer")

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "notify"
    assert row["targets"][0]["execution_result"] == "accepted"


# ── sleep_detection.py: nightly prompt fans out, one row per device ─────────

async def test_sleep_prompt_batches_one_row_per_device(
    load, fake_hass, isolated_db, al, monkeypatch,
):
    _stub_event_helpers(monkeypatch)
    sd = load("sleep_detection")
    fake_hass.services.register("notify", "mobile_app_a")
    fake_hass.services.register("notify", "mobile_app_b")
    fake_hass.services.register("notify", "mobile_app_unselected")

    await sd._send_sleep_prompt(fake_hass, "07:00", {
        "notify_services": '["notify.mobile_app_a", "notify.mobile_app_b"]',
    })

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "sleep_prompt"
    assert len(row["targets"]) == 2
    assert all(t["execution_result"] == "accepted" for t in row["targets"])
    called = {call[1] for call in fake_hass.service_calls if call[0] == "notify"}
    assert called == {"mobile_app_a", "mobile_app_b"}


async def test_sleep_prompt_no_devices_creates_no_row(load, fake_hass, isolated_db, al):
    sd = load("sleep_detection")
    fake_hass.services.register("notify", "mobile_app_unselected")
    await sd._send_sleep_prompt(fake_hass, "07:00", {"notify_services": "[]"})
    page = al.page_requests(limit=10, db_path=isolated_db)
    assert page["requests"] == []


# ── scenes.py: scene-by-intent is one logged action ──────────────────────────

async def test_scene_by_intent_logs_one_action(load, fake_hass, isolated_db, al):
    scenes_mod = load("scenes")
    fake_hass.states.set("scene.movie_night", "scening", friendly_name="Movie Night")
    fake_hass.services.register("scene", "turn_on")

    call = types.SimpleNamespace(
        data={"intent": "movie time", "announce": False},
        context=types.SimpleNamespace(user_id="user-1"),
    )

    class _Groq:
        def chat(self, messages, max_tokens=40, temperature=0.2):
            return {"text": "scene.movie_night"}

    result = await scenes_mod.async_activate_by_intent(
        fake_hass, call, _Groq(), "sir", None, [])
    assert result["success"] is True

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "scene_by_intent"
    assert row["source"] == "ha_service"
    assert row["targets"][0]["execution_result"] == "accepted"


# ── cognitive_core.py: LockdownManager.engage — locks + covers, one request ─

@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture(autouse=True)
def _isolate_lockdown_state(tmp_path, monkeypatch, cc):
    monkeypatch.setattr(cc, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))


async def test_lockdown_engage_locks_and_covers_share_one_request(
    cc, fake_hass, isolated_db, al,
):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")
    fake_hass.states.set("cover.garage_door", "open", device_class="garage",
                          friendly_name="Garage Door")
    mgr = cc.LockdownManager(fake_hass, {"honorific": "sir"})

    await mgr.engage("test")
    fake_hass.close_pending()  # don't run the background _verify_secured tasks

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "lockdown_engage"
    assert len(row["targets"]) == 2
    assert all(t["execution_result"] == "accepted" for t in row["targets"])
    entity_ids = {t["entity_id"] for t in row["targets"]}
    assert entity_ids == {"lock.front", "cover.garage_door"}


async def test_lockdown_breach_resecure_is_its_own_request(
    cc, fake_hass, isolated_db, al,
):
    """A breach re-secure during an ALREADY-active lockdown is a separate
    top-level trigger from engage() and must get its own request_id, not
    share engage()'s (they can't — engage() already completed)."""
    mgr = cc.LockdownManager(fake_hass, {"honorific": "sir"})
    mgr.active = True
    fake_hass.close_pending()

    old = fake_hass.states.set("lock.front", "locked", friendly_name="Front Lock")
    new = fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")

    await mgr.handle_state_change("lock.front", old, new)
    fake_hass.close_pending()

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "lockdown_breach_resecure"
    assert row["targets"][0]["entity_id"] == "lock.front"
    assert row["targets"][0]["execution_result"] == "accepted"


# ── cognitive_core.py: _emit_action → intrusion/cognitive alert dispatch ────

async def test_emit_action_single_push_is_logged_and_linked(cc, fake_hass, isolated_db, al):
    """sleeping=True + non-critical urgency takes _emit_action's phone-only
    branch (no TTS resolution needed), so this proves the notify-only push
    is logged without dragging in the TTS/audio_routing machinery."""
    fake_hass.services.register("notify", "mobile_app_test")

    action = {
        "type": "intrusion_investigating", "urgency": "high",
        "message": "Someone is in the house.", "notify_all": False,
    }
    await cc._emit_action(fake_hass, {"notify_service": "notify.mobile_app_test"},
                           action, sleeping=True)

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "cognitive_alert"
    assert row["targets"][0]["execution_result"] == "accepted"


async def test_emit_action_notify_all_fans_out_and_bundles_persistent_notification(
    cc, fake_hass, isolated_db, al,
):
    fake_hass.services.register("notify", "mobile_app_a")
    fake_hass.services.register("notify", "mobile_app_b")
    fake_hass.services.register("persistent_notification", "create")

    action = {
        "type": "intrusion_confirmed", "urgency": "critical",
        "message": "Confirmed intruder.", "notify_all": True,
    }
    await cc._emit_action(fake_hass, {}, action, sleeping=True)

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    targets = row["targets"]
    # 2 mobile_app targets + 1 persistent_notification target, all one request
    assert len(targets) == 3
    services = {t["service"] for t in targets}
    assert services == {"mobile_app_a", "mobile_app_b", "create"}
    assert all(t["execution_result"] == "accepted" for t in targets)


# ── cognitive_core.py: _execute_action_data for accepted proactive offers ──

async def test_execute_action_data_autonomous_is_logged(cc, fake_hass, isolated_db, al):
    fake_hass.services.register("light", "turn_on")
    ok = await cc._execute_action_data(
        fake_hass,
        {"domain": "light", "service": "turn_on", "entity_ids": ["light.den"]},
        source="proactive_autonomous",
    )
    assert ok is True
    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "proactive_offer_execute"
    assert row["source"] == "proactive_autonomous"
    assert row["targets"][0]["execution_result"] == "accepted"


async def test_execute_action_data_accepted_offer_is_logged(cc, fake_hass, isolated_db, al):
    fake_hass.services.register("climate", "set_preset_mode")
    cc._CORE.hass = fake_hass
    cc._CORE.pending_offer = {
        "action_data": {"domain": "climate", "service": "set_preset_mode",
                         "entity_ids": ["climate.house"], "service_data": {"preset_mode": "eco"}},
        "pattern_key": "",
    }
    result = await cc.accept_pending_offer()
    assert result["ok"] is True

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    assert page["requests"][0]["source"] == "proactive_accepted"


# ── local_engine.py: bulk "all" shortcut ─────────────────────────────────────

@pytest.fixture
def le(load):
    return load("local_engine")


async def test_local_engine_all_off_shortcut_batches_one_row_per_device(
    le, fake_hass, isolated_db, al,
):
    fake_hass.states.set("light.den", "on", friendly_name="Den")
    fake_hass.states.set("switch.fan", "on", friendly_name="Fan")
    fake_hass.services.register("light", "turn_off")
    fake_hass.services.register("switch", "turn_off")

    result = await le.try_local(fake_hass, "turn off everything", honorific="sir")
    assert result is not None and result.success

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "bulk_control"
    assert len(row["targets"]) == 2
    assert all(t["execution_result"] == "accepted" for t in row["targets"])


async def test_local_engine_bulk_area_shortcut_batches(le, fake_hass, isolated_db, al):
    fake_hass.states.set(
        "light.kitchen_1", "off", friendly_name="Kitchen 1",
        area_id="kitchen",
    )
    fake_hass.services.register("light", "turn_on")

    import unittest.mock as _um
    # _find_entities_in_area does a real area/entity registry lookup that the
    # synthetic HA stub doesn't provide -- monkeypatch it directly instead,
    # matching this module's own test conventions.
    with _um.patch.object(le, "_find_entities_in_area",
                           return_value=[("light.kitchen_1", "Kitchen 1")]):
        result = await le.try_local(fake_hass, "turn on all lights in kitchen", honorific="sir")

    assert result is not None
    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "bulk_control"
    assert len(row["targets"]) == 1
    assert row["targets"][0]["execution_result"] == "accepted"


# ── local_engine.py: scene/script fast path ──────────────────────────────────

async def test_local_engine_scene_fast_path_logs_one_action(le, fake_hass, isolated_db, al):
    fake_hass.states.set("scene.relax", "scening", friendly_name="Relax")
    fake_hass.services.register("scene", "turn_on")

    result = await le.try_local(fake_hass, "activate relax", honorific="sir")
    assert result is not None and result.success

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "scene_activation"
    assert row["targets"][0]["execution_result"] == "accepted"


# ── local_engine.py: goodnight shortcut ──────────────────────────────────────

async def test_local_engine_goodnight_scene_variant_logs_one_action(
    le, fake_hass, isolated_db, al,
):
    fake_hass.states.set("scene.goodnight", "scening", friendly_name="Goodnight")
    fake_hass.services.register("scene", "turn_on")

    result = await le.try_local(fake_hass, "goodnight", honorific="sir")
    assert result is not None and result.success

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    assert page["requests"][0]["action"] == "scene_activation"


async def test_local_engine_goodnight_manual_sweep_batches_lights_and_locks(
    le, fake_hass, isolated_db, al,
):
    """No scene.goodnight/script exists, so try_local falls through to the
    manual lights+locks sweep. cognitive_core._lockdown_exempt_locks() works
    fine against a fresh hass with no lockdown manager configured (returns
    the module default), so no extra patching is needed."""
    fake_hass.states.set("light.den", "on", friendly_name="Den")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.services.register("light", "turn_off")
    fake_hass.services.register("lock", "lock")

    result = await le.try_local(fake_hass, "goodnight", honorific="sir")
    assert result is not None and result.success
    fake_hass.close_pending()  # don't run the scheduled _verify_control tasks

    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "goodnight_sweep"
    assert len(row["targets"]) == 2
    assert all(t["execution_result"] == "accepted" for t in row["targets"])
