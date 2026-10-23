"""The intrusion flow seen from the outside: SafetyManager.tick() against a
fake house.

Motion while the residents are out, with an open way in or an armed alarm,
raises one "investigating" alert. After that Nova watches quietly and either
confirms (a route inward from the way in, sustained movement, or a person on
an indoor camera), sends one soft "please check" notice when nobody answers,
or closes the case as harmless. It must never become a stream of alerts.

Step by step branch tests live in test_cognitive_safety_investigation.py;
these drive whole ticks so the pieces are checked working together.
"""
from __future__ import annotations

import sys
import types

import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, intrusions  # noqa: F401

RESIDENT = "person.alex"
WAY_IN = "binary_sensor.hall_door"


def sensor(room: str) -> str:
    return f"binary_sensor.{room}_motion"


def room_of(entity_id: str) -> str:
    """The zone a test sensor stands for: binary_sensor.<room>_motion -> room."""
    return entity_id.split(".", 1)[1].removesuffix("_motion")


@pytest.fixture(autouse=True)
def _aiohttp_present(monkeypatch):
    """The camera lookup imports camera.py, which needs aiohttp. Other test
    files stub it as a side effect; doing it here keeps this file's result the
    same whether it runs alone or in the full suite."""
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))


@pytest.fixture
def intr(load, tmp_path, monkeypatch):
    """intrusion.py with an empty, private event log, so learned damping from
    another test can never quieten an alert here."""
    mod = load("intrusion")
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "intrusion_log.json")
    monkeypatch.setattr(mod, "_log", [])
    monkeypatch.setattr(mod, "_log_loaded", False)
    return mod


@pytest.fixture
def safety(cc, fake_hass, intr):
    return cc.SafetyManager(fake_hass, {"honorific": "sir"})


class House:
    """Drives one SafetyManager: set who is out, switch motion on and off,
    move the clock, and run a tick keeping only the intrusion actions."""

    def __init__(self, safety, hass, clock):
        self.safety, self.hass, self.clock = safety, hass, clock

    def everyone_out(self, opening: str | None = WAY_IN, kind: str = "door"):
        self.hass.states.set(RESIDENT, "not_home")
        if opening:
            self.hass.states.set(opening, "on", device_class=kind)

    def moving(self, room: str, active: bool = True):
        self.hass.states.set(sensor(room), "on" if active else "off", device_class="motion")

    def person_seen(self, entity_id: str):
        self.hass.states.set(entity_id, "on", device_class="occupancy")

    def wait(self, seconds: float):
        self.clock["now"] += seconds

    async def tick(self, anyone_home: bool = False) -> list:
        actions = await self.safety.tick(sleeping=False, anyone_home=anyone_home)
        self.hass.close_pending()
        return intrusions(actions)

    @property
    def case(self):
        return self.safety._investigation


def kinds(actions) -> list:
    return [a.get("type") for a in actions]


@pytest.fixture
def house(safety, fake_hass, clock):
    return House(safety, fake_hass, clock)


def floor_plan(safety, monkeypatch, load, *, breach, adjacent=(), hops=None):
    """Put each test sensor in the room its name says, set where the way in
    is, which rooms border it, and (optionally) each room's distance from it."""
    monkeypatch.setattr(safety, "_motion_key", room_of)
    monkeypatch.setattr(safety, "_breach_area", lambda entry: breach)
    graph = load("residence_graph")
    monkeypatch.setattr(graph, "adjacent_areas", lambda h, c, a: set(adjacent))
    monkeypatch.setattr(graph, "hops_from_breach", lambda h, c, a: dict(hops or {}))


def camera_sees_person(safety, monkeypatch, verdict, cam="camera.den"):
    """A person sensor reports someone on `cam`; Nova's own vision answers
    `verdict` (True, False or None for "could not tell"). Returns the list of
    cameras vision was asked about."""
    asked: list = []
    monkeypatch.setattr(safety, "_person_camera_entity", lambda indoor_only=True: cam)

    async def vision(entity):
        asked.append(entity)
        return verdict
    monkeypatch.setattr(safety, "_confirm_person_with_vision", vision)
    return asked


# ── one alert, then quiet ───────────────────────────────────────────────────

async def test_first_motion_raises_one_investigating_alert_then_stays_quiet(house):
    house.everyone_out()
    house.moving("lounge")
    opened = await house.tick()
    assert kinds(opened) == ["intrusion_investigating"]
    alert = opened[0]
    assert alert["urgency"] == "high"
    assert alert["auto_act"] is True
    assert alert["entity_id"] == sensor("lounge")
    assert alert["message"]
    # The same single sensor keeps firing: nothing more is said, the case stays open.
    for _ in range(3):
        house.wait(30)
        assert await house.tick() == []
    assert house.case is not None
    assert house.case["escalated"] is False


async def test_motion_with_no_way_in_and_no_alarm_raises_nothing(house):
    house.everyone_out(opening=None)
    house.moving("lounge")
    assert await house.tick() == []
    assert house.case is None


# ── what confirms an intrusion ──────────────────────────────────────────────

async def test_motion_from_the_breach_into_a_bordering_room_confirms(
        house, safety, monkeypatch, load):
    floor_plan(safety, monkeypatch, load, breach="lounge", adjacent={"landing"})
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    house.wait(20)
    house.moving("landing")
    confirmed = await house.tick()
    assert kinds(confirmed) == ["intrusion_confirmed"]
    alert = confirmed[0]
    assert alert["notify_all"] is True
    assert alert["urgency"] == "critical"
    assert alert["can_dismiss"] is True
    assert "moving inward" in alert["message"]
    assert house.case["path"] == ["lounge", "landing"]
    # Escalation happens once; more movement adds nothing.
    house.wait(10)
    house.moving("cellar")
    assert await house.tick() == []


async def test_two_zones_far_from_the_breach_are_watched_not_concluded(
        house, safety, monkeypatch, load):
    # Movement in two rooms, neither the way in nor next to it: the classic
    # false alarm pattern. Nova keeps watching instead of concluding.
    floor_plan(safety, monkeypatch, load, breach="garage", adjacent={"kitchen"})
    house.everyone_out()
    house.moving("attic")
    await house.tick()
    house.wait(20)
    house.moving("study")
    assert await house.tick() == []
    assert house.case is not None
    assert house.case["zones"] == {"attic", "study"}


async def test_without_a_known_breach_room_it_takes_sustained_movement(
        house, safety, monkeypatch):
    monkeypatch.setattr(safety, "_breach_area", lambda entry: None)
    monkeypatch.setattr(safety, "_motion_key", lambda eid: eid)
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    house.wait(20)
    house.moving("landing")
    assert await house.tick() == []                  # two zones, but only a blip
    house.wait(39)
    assert await house.tick() == []                  # 59 s: still short of a minute
    house.wait(1)
    confirmed = await house.tick()                   # 60 s: sustained
    assert kinds(confirmed) == ["intrusion_confirmed"]
    assert "sustained movement through the house while no one is home" in confirmed[0]["message"]


async def test_inward_progress_to_the_default_depth_confirms(house, safety, monkeypatch, load):
    floor_plan(safety, monkeypatch, load, breach="scullery",
               hops={"scullery": 0, "landing": 1, "lounge": 2})
    house.everyone_out(opening="binary_sensor.scullery_window", kind="window")
    house.moving("scullery")
    assert kinds(await house.tick()) == ["intrusion_investigating"]
    house.wait(20)
    house.moving("scullery", active=False)
    house.moving("landing")
    assert await house.tick() == []                  # one room in is not enough by default
    assert house.case["max_depth"] == 1
    house.wait(20)
    house.moving("landing", active=False)
    house.moving("lounge")
    out = await house.tick()
    assert kinds(out) == ["intrusion_confirmed"], \
        "motion reaching two rooms in from the way in must confirm"
    assert house.case["max_depth"] == 2


async def test_inward_depth_is_a_setting(house, safety, monkeypatch, load):
    safety.config["intrusion_inward_depth"] = 1
    floor_plan(safety, monkeypatch, load, breach="scullery", hops={"scullery": 0, "landing": 1})
    house.everyone_out(opening="binary_sensor.scullery_window", kind="window")
    house.moving("scullery")
    await house.tick()
    house.wait(20)
    house.moving("scullery", active=False)
    house.moving("landing")
    assert kinds(await house.tick()) == ["intrusion_confirmed"]


async def test_motion_that_lingers_at_the_way_in_never_confirms(
        house, safety, monkeypatch, load):
    # A fan in an open window or a curtain keeps the breach room busy for
    # minutes; it never reaches a room further in, so it never confirms.
    floor_plan(safety, monkeypatch, load, breach="scullery", hops={"scullery": 0})
    monkeypatch.setattr(safety, "_person_camera_entity", lambda indoor_only=True: None)
    house.everyone_out(opening="binary_sensor.scullery_window", kind="window")
    house.moving("scullery")
    await house.tick()
    for _ in range(5):
        house.wait(30)
        house.moving("scullery")
        out = await house.tick()
        assert "intrusion_confirmed" not in kinds(out), \
            "motion lingering at the breach must never confirm an intrusion"
    assert house.case["max_depth"] == 0


# ── cameras ─────────────────────────────────────────────────────────────────

async def test_an_indoor_person_sensor_confirms(house):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    house.wait(20)
    house.person_seen("binary_sensor.front_cam_person")
    assert kinds(await house.tick()) == ["intrusion_confirmed"]


async def test_an_outdoor_person_sensor_never_confirms_but_an_indoor_one_does(house):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    house.wait(20)
    # A courier on the driveway camera is a doorstep event, not proof of entry.
    house.person_seen("binary_sensor.driveway_cam_person")
    assert await house.tick() == []
    assert house.case is not None
    house.wait(10)
    house.person_seen("binary_sensor.hallway_cam_person")
    assert kinds(await house.tick()) == ["intrusion_confirmed"]


async def test_vision_agreeing_with_the_camera_confirms(house, safety, monkeypatch):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    asked = camera_sees_person(safety, monkeypatch, True)
    house.wait(20)
    out = await house.tick()
    assert kinds(out) == ["intrusion_confirmed"]
    assert "a person is on camera (confirmed by vision)" in out[0]["message"]
    assert asked == ["camera.den"]


async def test_vision_overruling_the_camera_holds_the_alarm(house, safety, monkeypatch):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    asked = camera_sees_person(safety, monkeypatch, False)
    house.wait(20)
    # One zone, not sustained: with the camera discounted nothing confirms.
    assert await house.tick() == []
    assert house.case is not None
    assert house.case["escalated"] is False
    assert asked == ["camera.den"]


async def test_vision_that_cannot_tell_trusts_the_camera(house, safety, monkeypatch):
    # Fail safe: a broken or unsure vision check must never hide a real alert.
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    camera_sees_person(safety, monkeypatch, None)
    house.wait(20)
    out = await house.tick()
    assert kinds(out) == ["intrusion_confirmed"]
    assert "a person is on camera." in out[0]["message"]
    assert "confirmed by vision" not in out[0]["message"]


async def test_vision_switched_off_gives_no_verdict(safety):
    safety.config["intrusion_vision_confirm"] = False
    assert await safety._confirm_person_with_vision("camera.den") is None


@pytest.mark.parametrize("handle", ["", None, "binary_sensor.den_person", "sensor.camera_den"])
async def test_vision_needs_a_real_camera_entity(safety, handle):
    assert await safety._confirm_person_with_vision(handle) is None


@pytest.mark.parametrize("answer, verdict", [
    ("person: yes", True),
    ("person: no", False),
    ("person: no. a person is reflected in the glass", False),   # the tag wins
    ("the room is empty", False),
    ("nobody is there", False),
    ("someone is standing by the sofa", True),
    ("an intruder near the stairs", True),
    ("not a person, just a coat", None),      # reads as both, so unsure
    ("a cat on the rug", None),
])
def test_how_a_vision_answer_is_read(cc, answer, verdict):
    reader = sys.modules[cc.SafetyManager.__module__]._vision_verdict
    assert reader(answer) is verdict


# ── what does not count as a way in or as motion ────────────────────────────

async def test_outdoor_motion_never_opens_an_investigation(house):
    house.everyone_out()
    house.moving("patio")
    assert await house.tick() == []
    assert house.case is None


async def test_an_open_yard_gate_is_no_breach_but_the_garage_is(house, fake_hass):
    house.everyone_out(opening=None)
    fake_hass.states.set("cover.side_gate", "open", device_class="gate")
    house.moving("lounge")
    assert await house.tick() == []
    assert house.case is None
    fake_hass.states.set("cover.garage_door", "open", device_class="garage")
    assert kinds(await house.tick()) == ["intrusion_investigating"]


# ── closing a case ──────────────────────────────────────────────────────────

async def test_quiet_motion_closes_the_case_only_after_the_quiet_window(house, cc):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    house.moving("lounge", active=False)
    house.wait(cc.INTRUSION_CLEAR_QUIET_SECS)
    assert await house.tick() == []
    assert house.case is not None                   # exactly at the window: still open
    house.wait(1)
    assert await house.tick() == []
    assert house.case is None                       # past it: harmless, no alert


async def test_residents_coming_home_end_the_investigation(house, fake_hass):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    assert house.case is not None
    fake_hass.states.set(RESIDENT, "home")
    house.wait(10)
    assert await house.tick(anyone_home=True) == []
    assert house.case is None


# ── nobody answers ──────────────────────────────────────────────────────────

async def test_unanswered_ongoing_motion_sends_one_soft_notice_not_a_confirmation(house, cc):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    house.wait(cc.INTRUSION_RESPONSE_TIMEOUT_SECS + 5)
    house.moving("lounge")
    out = await house.tick()
    assert kinds(out) == ["intrusion_unresolved"], \
        "unanswered active motion with no route must send a soft notice only"
    notice = out[0]
    assert notice["urgency"] == "high"
    assert notice["notify_all"] is True
    assert "couldn't reach you" in notice["message"].lower()
    assert "not confirmed" in notice["message"].lower()
    house.wait(10)
    house.moving("lounge")
    assert await house.tick() == []                 # only ever one soft notice


async def test_an_acknowledgement_holds_the_unanswered_notice(house, intr, cc):
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    intr.acknowledge("on my way")
    house.wait(cc.INTRUSION_RESPONSE_TIMEOUT_SECS + 5)
    house.moving("lounge")
    assert await house.tick() == [], \
        "an acknowledgement must hold both the soft notice and any escalation"
    assert house.case is not None


# ── learned damping ─────────────────────────────────────────────────────────

def _learned_false_alarm(intr, monkeypatch):
    monkeypatch.setattr(intr, "should_damp_weak_alert", lambda area, camera: True)
    monkeypatch.setattr(intr, "record_event", lambda *a, **k: {})


async def test_a_learned_false_alarm_quietens_the_first_alert_only(
        house, safety, intr, monkeypatch, load):
    _learned_false_alarm(intr, monkeypatch)
    floor_plan(safety, monkeypatch, load, breach="scullery", hops={"scullery": 0})
    house.everyone_out(opening="binary_sensor.scullery_window", kind="window")
    house.moving("scullery")
    assert await house.tick() == [], "a damped pattern stays quiet on the first ping"
    assert house.case is not None, "the investigation still runs underneath"


async def test_learned_damping_never_hides_a_confirmed_intrusion(
        house, safety, intr, monkeypatch, load):
    _learned_false_alarm(intr, monkeypatch)
    floor_plan(safety, monkeypatch, load, breach="scullery",
               hops={"scullery": 0, "landing": 1, "lounge": 2})
    house.everyone_out(opening="binary_sensor.scullery_window", kind="window")
    house.moving("scullery")
    await house.tick()
    for previous, room in (("scullery", "landing"), ("landing", "lounge")):
        house.wait(20)
        house.moving(previous, active=False)
        house.moving(room)
        out = await house.tick()
    assert kinds(out) == ["intrusion_confirmed"], \
        "learned damping must never suppress a confirmed intrusion"


# ── reporting ───────────────────────────────────────────────────────────────

async def test_status_reports_the_breach_and_route(house, safety, monkeypatch, load, cc):
    cc._CORE.safety_mgr = None
    assert cc.intrusion_status() == {"active": False, "confirmed": False}
    floor_plan(safety, monkeypatch, load, breach="lounge")
    house.everyone_out()
    house.moving("lounge")
    await house.tick()
    cc._CORE.safety_mgr = safety                     # restored by _isolated_core
    status = cc.intrusion_status()
    assert status["active"] is True
    assert status["confirmed"] is False
    assert status["breach_area"] == "lounge"
    assert status["path"] == ["lounge"]
    assert status["zones"] == ["lounge"]


async def test_a_confirmed_alert_reaches_every_phone_and_the_dashboard(cc, fake_hass):
    for name in ("mobile_app_phone_a", "mobile_app_phone_b", "slack"):
        fake_hass.services.register("notify", name)
    await cc._notify_all_devices(fake_hass, {}, "intrusion!", "intrusion_confirmed")
    pushes = [c for c in fake_hass.service_calls if c[0] == "notify"]
    assert sorted(c[1] for c in pushes) == ["mobile_app_phone_a", "mobile_app_phone_b"]
    for _domain, _name, payload in pushes:
        assert payload["message"] == "intrusion!"
        assert payload["title"] == "Nova — INTRUSION"
    banners = [c for c in fake_hass.service_calls if c[0] == "persistent_notification"]
    assert len(banners) == 1
    assert banners[0][2]["notification_id"] == "nova_intrusion"


async def test_an_investigating_alert_puts_no_banner_on_the_dashboard(cc, fake_hass):
    fake_hass.services.register("notify", "mobile_app_phone_a")
    await cc._notify_all_devices(fake_hass, {}, "checking", "intrusion_investigating")
    assert [c[0] for c in fake_hass.service_calls] == ["notify"]


async def test_with_no_phone_apps_the_configured_service_is_used(cc, fake_hass):
    await cc._notify_all_devices(
        fake_hass, {"notify_service": "notify.fallback"}, "hi", "intrusion_investigating")
    assert fake_hass.service_calls == [
        ("notify", "fallback", {"message": "hi", "title": "Nova — Security Alert"})]
