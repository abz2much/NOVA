"""Notifications given three days running go quiet; emergencies never do."""
import datetime
import json

import pytest
import time as _time
from test_cognition_predict_evidence import _seed


def _ts(day, hour=21):
    return datetime.datetime(2026, 10, day, hour, 0).timestamp()


@pytest.fixture
def hab(load, tmp_path, monkeypatch):
    h = load("habituation")
    monkeypatch.setattr(h, "STATE_FILE", str(tmp_path / "habituation.json"))
    monkeypatch.setattr(h, "_state", None)
    return h


def test_third_day_running_carries_the_note_then_goes_quiet(hab):
    key = "binary_sensor.disk"
    for day in (1, 2):
        assert hab.with_note(key, "Disk busy.", _ts(day)) == "Disk busy."
        hab.record(key, key, _ts(day))
        assert not hab.is_quiet(key)
    assert hab.with_note(key, "Disk busy.", _ts(3)) == "Disk busy. " + hab.QUIET_NOTE
    hab.record(key, key, _ts(3))
    assert hab.is_quiet(key)


def test_same_day_repeats_count_once(hab):
    key = "switch.light"
    for day in (1, 2):
        for hour in (20, 21):
            hab.record(key, key, _ts(day, hour))
    assert not hab.is_quiet(key)


def test_a_missed_day_starts_the_count_again(hab):
    key = "binary_sensor.disk"
    for day in (1, 2, 4, 5):
        hab.record(key, key, _ts(day))
    assert not hab.is_quiet(key)
    assert hab.with_note(key, "x", _ts(6)).endswith(hab.QUIET_NOTE)
    hab.record(key, key, _ts(6))
    assert hab.is_quiet(key)


def test_quiet_survives_a_restart(hab, monkeypatch):
    for day in (1, 2, 3):
        hab.record("k", "sensor.a", _ts(day))
    monkeypatch.setattr(hab, "_state", None)
    assert hab.is_quiet("k")
    with open(hab.STATE_FILE) as f:
        assert json.load(f)["k"]["quiet"] is True


def test_forget_brings_it_back(hab):
    for day in (1, 2, 3):
        hab.record("routine:p:switch.front_light:on", "switch.front_light", _ts(day))
    assert hab.forget("switch.front_*") == ["routine:p:switch.front_light:on"]
    assert not hab.is_quiet("routine:p:switch.front_light:on")
    assert hab.quiet_list() == []


@pytest.mark.parametrize("kw", [
    {"urgency": "critical"},
    {"kind": "intrusion_investigating"},
    {"kind": "lockdown_breach"},
    {"kind": "freeze_warning", "urgency": "high"},
    {"entity_id": "lock.back_door"},
    {"entity_id": "alarm_control_panel.home"},
    {"kind": "package_delivered", "entity_id": "camera.front_door"},
    {"kind": "package_removed", "entity_id": "camera.front_door"},
    {"kind": "doorbell", "entity_id": "camera.doorbell"},
    {"kind": "camera", "entity_id": "camera.driveway"},
    {"kind": "door_left_open", "entity_id": "binary_sensor.back_door"},
    {"kind": "window_left_open", "entity_id": "binary_sensor.bedroom_window"},
    {"kind": "garage_left_open", "entity_id": "binary_sensor.garage_door"},
    {"kind": "doors_windows", "urgency": "high", "entity_id": "binary_sensor.patio"},
    {"kind": "security", "urgency": "medium", "entity_id": "binary_sensor.hall_motion"},
])
def test_emergencies_are_exempt(hab, kw):
    assert hab.exempt(**kw)


def test_ordinary_alerts_are_not_exempt(hab):
    assert not hab.exempt(urgency="high", kind="problem", entity_id="binary_sensor.disk")
    assert not hab.exempt(urgency="low", kind="anticipation_routine", entity_id="switch.x")


@pytest.mark.parametrize("category,entity_id", [
    ("package_delivered", "camera.front_door"),
    ("package_removed", "camera.front_door"),
    ("doorbell", "camera.doorbell"),
    ("camera", "camera.driveway"),
    ("door_left_open", "binary_sensor.back_door"),
    ("doors_windows", "binary_sensor.patio"),
    ("security", "binary_sensor.hall_motion"),
])
def test_output_gate_never_quietens_these(hab, load, monkeypatch, category, entity_id):
    og = load("output_gate")
    og._STATE.history.clear()
    og._STATE.recent_messages.clear()
    for day in (1, 2, 3, 4):
        monkeypatch.setattr(og, "_now", lambda d=day: _ts(d))
        monkeypatch.setattr(hab.time, "time", lambda d=day: _ts(d))
        ok, _ = og.can_announce(entity_id=entity_id, category=category,
                                urgency="medium", message=f"Alert {day}")
        assert ok
        msg = og.habit_note(entity_id=entity_id, category=category,
                            urgency="medium", message="Alert.")
        assert hab.QUIET_NOTE not in msg
        og.record_announcement(entity_id=entity_id, category=category,
                               urgency="medium", message=msg, was_spoken=True)
        og._STATE.recent_messages.clear()
    assert not hab.is_quiet(entity_id)


def test_a_package_alert_already_quiet_comes_back(hab, load):
    og = load("output_gate")
    for day in (1, 2, 3):
        hab.record("camera.front_door", "camera.front_door", _ts(day))
    assert hab.is_quiet("camera.front_door")
    ok, _ = og.can_announce(entity_id="camera.front_door", category="package_delivered",
                            urgency="medium", message="A package has been delivered.")
    assert ok


def test_output_gate_goes_quiet_after_three_spoken_days(hab, load, monkeypatch):
    og = load("output_gate")
    monkeypatch.setattr(og, "_now", lambda: _ts(1))
    og._STATE.history.clear()
    og._STATE.recent_messages.clear()
    for day in (1, 2, 3):
        monkeypatch.setattr(hab.time, "time", lambda d=day: _ts(d))
        ok, _ = og.can_announce(entity_id="binary_sensor.disk", category="problem",
                                urgency="high", message="Disk busy")
        assert ok
        msg = og.habit_note(entity_id="binary_sensor.disk", category="problem",
                            urgency="high", message="Disk busy.")
        assert msg.endswith(hab.QUIET_NOTE) == (day == 3)
        og.record_announcement(entity_id="binary_sensor.disk", category="problem",
                               urgency="high", message=msg, was_spoken=True)
        og._STATE.recent_messages.clear()
    ok, reason = og.can_announce(entity_id="binary_sensor.disk", category="problem",
                                 urgency="high", message="Disk busy")
    assert not ok and "normal for this home" in reason
    ok, _ = og.can_announce(entity_id="binary_sensor.disk", category="problem",
                            urgency="critical", message="Disk busy")
    assert ok


def test_unignore_restores_a_quiet_notification(hab, load):
    core = load("cognitive_core")
    for day in (1, 2, 3):
        hab.record("binary_sensor.disk", "binary_sensor.disk", _ts(day))
    core._CORE.ignore_mgr = None
    assert [r["pattern"] for r in core.list_ignores()] == ["binary_sensor.disk"]
    out = core.unignore("binary_sensor.disk")
    assert out["success"] is True and out["restored_notifications"] == 1
    assert not hab.is_quiet("binary_sensor.disk")


async def test_save_on_the_event_loop_runs_in_the_executor(hab, monkeypatch):
    """record() is called from async alert code: the file write must not run
    on the loop thread."""
    import asyncio
    import threading
    loop_thread = threading.get_ident()
    writers = []
    real_write = hab._write

    def spy(snapshot, gen):
        real_write(snapshot, gen)
        writers.append(threading.get_ident())

    monkeypatch.setattr(hab, "_write", spy)
    hab.record("k", "k", _ts(1))
    for _ in range(50):
        if writers:
            break
        await asyncio.sleep(0.01)
    assert writers and loop_thread not in writers
    with open(hab.STATE_FILE) as f:
        assert json.load(f)["k"]["streak"] == 1


def test_an_older_snapshot_never_overwrites_a_newer_one(hab, monkeypatch):
    monkeypatch.setattr(hab, "_written_gen", 0)
    hab._write({"k": {"streak": 2}}, 2)
    hab._write({"k": {"streak": 1}}, 1)
    with open(hab.STATE_FILE) as f:
        assert json.load(f)["k"]["streak"] == 2


# ── lock, cover and alarm alerts never go quiet ───────────────────────────────

DAY = 86400.0


@pytest.fixture
def cog(load):
    c = load("cognition")
    c._MODEL.clear()
    c._PREDICT_COOLDOWNS.clear()
    yield c
    c._MODEL.clear()
    c._PREDICT_COOLDOWNS.clear()


@pytest.fixture
def days(load, hab, fake_hass, monkeypatch):
    """Deliver through _emit_action on successive days; returns a function
    giving what was delivered (spoken or pushed) on each day."""
    cc = load("cognitive_core")
    clock = {"now": _time.time()}
    monkeypatch.setattr(hab.time, "time", lambda: clock["now"])
    out = []

    async def _push(hass, config, message, action_type, snap=None, **kw):
        out.append(message)
        return ["notify.phone"]
    monkeypatch.setattr(cc, "_push_notification", _push)
    monkeypatch.setattr(cc, "_notify_all_devices", _push)
    tts = load("tts_helper")
    monkeypatch.setattr(tts, "resolve_tts_for_context", lambda *a, **k: "tts.x")

    async def _announce(hass, message, *a, **k):
        out.append(message)
        return True
    monkeypatch.setattr(tts, "async_announce", _announce)
    monkeypatch.setattr(load("sleep_detection"), "_in_quiet_hours", lambda *a: False)
    # The output gate (8.45.0) starts clean and keeps the same days.
    og = load("output_gate")
    monkeypatch.setattr(og, "_STATE", og.GateState())
    monkeypatch.setattr(og, "_now", lambda: clock["now"])
    monkeypatch.setattr(load("database"), "save_activity", lambda **kw: None)
    fake_hass.states.set("media_player.hall", "idle")

    async def run(action, n=4):
        delivered = []
        for _ in range(n):
            out.clear()
            await cc._emit_action(fake_hass, {"broadcast_group": "media_player.hall"},
                                  dict(action), False)
            delivered.append(list(out))
            clock["now"] += DAY
        return delivered
    return run


def _predict_one(cog, fake_hass, eid, current, dominant, **attrs):
    now = _time.time()
    fake_hass.states.set(eid, current, **attrs)
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, eid, now, dominant_state=dominant, current_state=current)
    out = cog.predict(fake_hass, now)
    assert len(out) == 1, out
    return out[0]


async def test_the_anticipation_alert_carries_its_entity(cog, fake_hass):
    alert = _predict_one(cog, fake_hass, "lock.front_door", "unlocked", "locked",
                         friendly_name="Front Door")
    assert alert["entity_id"] == "lock.front_door"
    assert alert["message"] == "Front Door is unlocked while nobody appears to be home."


async def test_door_unlocked_while_away_stays_loud_on_day_four(cog, fake_hass, days):
    alert = _predict_one(cog, fake_hass, "lock.front_door", "unlocked", "locked",
                         friendly_name="Front Door")
    delivered = await days(alert)
    assert all(d for d in delivered), delivered
    assert delivered[3] and "stop mentioning" not in " ".join(sum(delivered, []))


async def test_garage_open_while_away_stays_loud_on_day_four(cog, fake_hass, days):
    alert = _predict_one(cog, fake_hass, "cover.garage_door", "open", "closed",
                         device_class="garage", friendly_name="Garage Door")
    assert alert["entity_id"] == "cover.garage_door"
    delivered = await days(alert)
    assert all(d for d in delivered), delivered


async def test_an_alarm_alert_stays_loud_on_day_four(days):
    delivered = await days({"type": "anticipation", "urgency": "medium",
                            "message": "The alarm is disarmed while nobody appears to be home.",
                            "pattern_key": "anticipate:alarm_control_panel.home",
                            "entity_id": "alarm_control_panel.home", "offer": False})
    assert all(d for d in delivered), delivered


async def test_an_alert_that_already_went_quiet_comes_back(hab, days):
    hab._load()["anticipate:lock.front_door"] = {
        "entity_id": "", "last_day": 1, "streak": 3, "quiet": True}
    delivered = await days({"type": "anticipation", "urgency": "medium",
                            "message": "Front Door is unlocked while nobody appears to be home.",
                            "pattern_key": "anticipate:lock.front_door",
                            "entity_id": "lock.front_door", "offer": False}, n=1)
    assert delivered[0] and set(delivered[0]) == {
        "Front Door is unlocked while nobody appears to be home."}


async def test_other_alerts_still_go_quiet_after_three_days(days):
    # Unchanged: a light is not a lock, cover or alarm.
    delivered = await days({"type": "anticipation_routine", "urgency": "medium",
                            "message": "The porch light is on.",
                            "pattern_key": "anticipate:light.porch",
                            "entity_id": "light.porch", "offer": False})
    assert delivered[0] and delivered[1]
    assert "stop mentioning" in " ".join(delivered[2])
    assert delivered[3] == []


def test_exempt_covers_every_cover(load):
    hab = load("habituation")
    for eid in ("cover.garage_door", "cover.gate", "cover.living_blind"):
        assert hab.exempt(urgency="low", kind="anticipation", entity_id=eid)
