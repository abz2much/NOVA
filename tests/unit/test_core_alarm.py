"""The alarm going off, announced by the cognitive core (8.83.0).

Before, the observer turned the alarm going off into "a security alert has
been triggered. Immediate attention required." with nothing about where, and
said the same when the alarm only started its entry countdown (pending) as a
resident came home. Now the core gives one alert for the chosen alarm going
off: which sensor tripped (the alarm's open_sensors), and what the camera
for that room shows, checked by Nova's vision. A slow or broken camera check
never holds the alert up for long, and never stops it.

All state here is fake. Nothing touches a real alarm.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

ALARM = "alarm_control_panel.home_security"


def _state(state, open_sensors=None):
    attrs = {"friendly_name": "Alarmo Home Security"}
    if open_sensors is not None:
        attrs["open_sensors"] = open_sensors
    return SimpleNamespace(state=state, attributes=attrs,
                           last_changed=datetime(2026, 10, 27, 12, 0))


def _event(old, new, eid=ALARM, open_sensors=None):
    return SimpleNamespace(data={"entity_id": eid, "old_state": _state(old),
                                 "new_state": _state(new, open_sensors)})


class _Hass:
    def __init__(self):
        self.tasks = []
        self.states = SimpleNamespace(get=lambda eid: SimpleNamespace(
            attributes={"friendly_name": {"binary_sensor.back_door": "Back Door"}.get(eid, eid)}))

    def async_create_task(self, coro, name=None):
        self.tasks.append(coro)
        coro.close()


class _Safety:
    """What the safety manager answers about the camera for a sensor."""

    def __init__(self, answer=None, slow=False, broken=False):
        self.answer = answer or {}
        self.slow, self.broken = slow, broken
        self.asked = []

    async def camera_answer(self, entity_id):
        self.asked.append(entity_id)
        if self.broken:
            raise RuntimeError("camera gone")
        if self.slow:
            await asyncio.sleep(60)
        return self.answer


@pytest.fixture
def ca(load, monkeypatch):
    mod = load("core_alarm")
    monkeypatch.setattr(load("alarm_source"), "is_selected",
                        lambda hass, eid, cfg=None: eid == ALARM)
    monkeypatch.setattr(load("core_common"), "_live_honorific", lambda h: "")
    monkeypatch.setattr(mod, "CAMERA_WAIT_S", 0.05)
    return mod


# ── what counts as going off ────────────────────────────────────────────────

@pytest.mark.parametrize("old", ["armed_away", "armed_home", "armed_night", "pending"])
def test_the_chosen_alarm_going_off(ca, old):
    assert ca.went_off(_Hass(), {}, _event(old, "triggered")) is True


@pytest.mark.parametrize("old,new,eid", [
    ("armed_away", "pending", ALARM),          # the entry countdown, not an alarm
    ("triggered", "triggered", ALARM),         # still going off
    ("unavailable", "triggered", ALARM),       # a restart, not a new alarm
    ("unknown", "triggered", ALARM),
    ("armed_away", "triggered", "alarm_control_panel.back_homebase"),   # not the chosen one
])
def test_anything_else_is_not_going_off(ca, old, new, eid):
    assert ca.went_off(_Hass(), {}, _event(old, new, eid)) is False


def test_on_state_changed_schedules_one_alert(ca):
    hass = _Hass()
    assert ca.on_state_changed(hass, {}, _event("armed_away", "triggered"))
    assert not ca.on_state_changed(hass, {}, _event("armed_away", "pending"))
    assert len(hass.tasks) == 1


@pytest.mark.parametrize("value,expected", [
    ({"binary_sensor.back_door": "on"}, ["binary_sensor.back_door"]),
    (["binary_sensor.back_door"], ["binary_sensor.back_door"]),
    (None, []), ("nonsense", []),
])
def test_the_tripped_sensors(ca, value, expected):
    assert ca.tripped(_state("triggered", value)) == expected


# ── the alert ───────────────────────────────────────────────────────────────

async def _action(ca, safety, open_sensors=None):
    sensors = {"binary_sensor.back_door": "on"} if open_sensors is None else open_sensors
    return await ca.alarm_action(_Hass(), {}, _state("triggered", sensors), safety)


async def test_a_person_on_camera(ca):
    safety = _Safety({"vision": True, "camera": "camera.garden",
                      "snapshot_url": "/local/x.jpg", "snapshot_path": "/config/www/x.jpg"})
    action = await _action(ca, safety)
    assert safety.asked == ["binary_sensor.back_door"]
    assert action["type"] == "alarm_triggered" and action["urgency"] == "critical"
    assert action["notify_all"] is True and action["entity_id"] == "binary_sensor.back_door"
    assert "Back Door" in action["message"] and "camera sees a person" in action["message"]
    assert action["snapshot_url"] == "/local/x.jpg" and action["camera"] == "camera.garden"


async def test_no_one_on_camera(ca):
    action = await _action(ca, _Safety({"vision": False, "camera": "camera.garden"}))
    assert "Back Door" in action["message"] and "No one is on the camera" in action["message"]


async def test_a_camera_that_could_not_tell_says_so(ca):
    action = await _action(ca, _Safety({"vision": None, "camera": "camera.garden"}))
    assert "couldn't tell from the camera" in action["message"]


async def test_no_camera_for_that_room_says_only_where(ca):
    action = await _action(ca, _Safety({}))
    assert "Back Door" in action["message"] and "camera" not in action["message"].lower()


@pytest.mark.parametrize("safety", [_Safety(slow=True), _Safety(broken=True), None])
async def test_a_slow_broken_or_missing_camera_check_never_stops_the_alert(ca, safety):
    action = await _action(ca, safety)
    assert action["urgency"] == "critical" and "Back Door" in action["message"]


async def test_no_sensor_named_still_alerts(ca):
    action = await _action(ca, _Safety({}), open_sensors={})
    assert action["urgency"] == "critical" and action["message"]
    assert "entity_id" not in action or not action["entity_id"]


def test_the_core_announces_it_before_ignore_rules():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2]
            / "custom_components" / "nova" / "core_runtime.py").read_text(encoding="utf-8")
    hazard = text.index("_m_hazards.on_state_changed(")
    alarm = text.index("_m_alarm.on_state_changed(")
    ignore = text.index("core.ignore_mgr.is_ignored(")
    assert hazard < alarm < ignore


# ── the safety manager's camera answer ──────────────────────────────────────

async def test_no_camera_for_the_room_is_an_empty_answer(cognitive_core, fake_hass, monkeypatch):
    safety = cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})
    monkeypatch.setattr(safety, "_camera_for", lambda inv: None)
    assert await safety.camera_answer("binary_sensor.back_door") == {}


async def test_the_camera_answer_carries_vision_and_the_snapshot(cognitive_core, fake_hass,
                                                                monkeypatch):
    safety = cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})
    monkeypatch.setattr(safety, "_breach_area", lambda eid: "garden")
    asked = []
    monkeypatch.setattr(safety, "_camera_for",
                        lambda inv: asked.append(inv["breach_area"]) or "camera.garden")

    async def vision(cam):
        return False

    async def snapshot(cam):
        return {"path": "/config/www/x.jpg", "camera": cam}

    async def attach(action, snap):
        action.update(snapshot_url="/local/x.jpg", snapshot_path=snap["path"])
        return "/local/x.jpg"
    monkeypatch.setattr(safety, "_confirm_person_with_vision", vision)
    monkeypatch.setattr(safety, "_snapshot", snapshot)
    monkeypatch.setattr(safety, "_attach_snapshot", attach)
    answer = await safety.camera_answer("binary_sensor.back_door")
    assert asked == ["garden"]
    assert answer == {"camera": "camera.garden", "vision": False,
                      "snapshot_url": "/local/x.jpg", "snapshot_path": "/config/www/x.jpg"}


def test_the_phone_alert_has_a_security_title_in_every_language(load):
    i18n = load("notify_i18n")
    for lang in i18n.TITLES["intrusion_investigating"]:
        assert i18n.title("alarm_triggered", lang) == i18n.title("intrusion_investigating", lang)
    assert i18n.title("alarm_triggered", "en") == "Nova — Security Alert"
