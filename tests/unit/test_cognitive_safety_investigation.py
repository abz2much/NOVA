"""_check_intrusion, _begin_investigation, _investigate_step and the vision
second opinion, branch by branch.

Characterisation tests (8.7.15). The flow level tests live in
test_intrusion_investigation.py, test_intrusion_confinement.py,
test_sleeping_intrusion.py and test_face_stand_down.py; these call the steps
directly so each decision (escalate exactly once, clear, hold, call off) is
pinned on its own. test_current_behaviour_* tests pin something odd that is
not being changed here (see "Found, not fixed" in the PR).
"""
import sys
import types

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, away, cc, clock, intrusions, motion)

ALARM = "alarm_control_panel.home"


@pytest.fixture
def intr(load):
    return load("intrusion")


@pytest.fixture
def safety(cc, fake_hass):
    return cc.SafetyManager(fake_hass, {"honorific": "sir"})


@pytest.fixture
def records(intr, monkeypatch):
    """Capture intrusion.record_event calls: [(kind, kwargs)]."""
    seen = []
    monkeypatch.setattr(intr, "record_event",
                        lambda kind, **kw: seen.append((kind, kw)))
    return seen


def _seed(safety, now, **over):
    """An investigation that is already open, as _begin_investigation builds it."""
    inv = {"start": now, "last_motion": now, "zones": {"living"}, "path": ["living"],
           "escalated": False, "breach_area": "living", "breach_name": "Front Door",
           "connected": {"living", "hall"}, "hops": {}, "max_depth": 0,
           "trigger": "away"}
    inv.update(over)
    safety._investigation = inv
    return inv


def _plan(safety, monkeypatch, load, mapping=None, breach="living", adjacent=(), hops=None):
    """Give sensors areas, set the breach and its adjacent rooms and depths."""
    mapping = mapping or {}
    monkeypatch.setattr(safety, "_motion_key", lambda eid: mapping.get(eid, eid))
    monkeypatch.setattr(safety, "_breach_area", lambda e: breach)
    rg = load("residence_graph")
    monkeypatch.setattr(rg, "adjacent_areas", lambda h, c, a: set(adjacent))
    monkeypatch.setattr(rg, "hops_from_breach", lambda h, c, a: dict(hops or {}))


def _one_zone(safety, monkeypatch, load):
    """The default motion sensor lives in the breach room, so motion never adds
    a second zone and nothing about the route can confirm an intrusion."""
    _plan(safety, monkeypatch, load, mapping={"binary_sensor.living_motion": "living"})
    _no_camera(safety, monkeypatch)


def _in_breach_room(safety, monkeypatch):
    """Keep the default motion sensor in the breach room unless a test maps it."""
    monkeypatch.setattr(safety, "_motion_key", lambda eid: (
        "living" if eid == "binary_sensor.living_motion" else eid))


def _no_camera(safety, monkeypatch):
    _in_breach_room(safety, monkeypatch)
    monkeypatch.setattr(safety, "_person_camera_entity", lambda indoor_only=True: None)


# ── _check_intrusion ────────────────────────────────────────────────────────

async def test_away_motion_needs_an_open_entry_or_an_armed_alarm_before_anything_starts(
        cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.username", "not_home")
    fake_hass.states.set(ALARM, "disarmed")
    motion(fake_hass)
    assert await safety._check_intrusion(False, False) is None           # bare motion: a pet, a robot, a blind
    assert safety._investigation is None and safety._last_intrusion_alert == 0.0
    fake_hass.states.set(ALARM, "armed_away")
    assert (await safety._check_intrusion(False, False))["type"] == "intrusion_investigating"


async def test_called_off_means_no_new_alert(safety, fake_hass, clock, intr):
    away(fake_hass)
    motion(fake_hass)
    intr.dismiss_intrusion("false alarm")
    assert await safety._check_intrusion(False, False) is None
    assert safety._last_intrusion_alert == 0.0                      # nothing was started


async def test_current_behaviour_a_call_off_freezes_an_open_investigation_instead_of_dropping_it(
        safety, fake_hass, clock, intr):
    """_check_intrusion returns before it ever steps the investigation while a
    call off is in force, so the open investigation sits untouched (and still
    reported as active by intrusion_status) until the call off lapses."""
    away(fake_hass)
    motion(fake_hass)
    assert (await safety._check_intrusion(False, False))["type"] == "intrusion_investigating"
    inv = safety._investigation
    intr.dismiss_intrusion("false alarm")
    clock["now"] += 30
    assert await safety._check_intrusion(False, False) is None
    assert safety._investigation is inv                             # not cleared
    assert inv["escalated"] is False


async def test_an_open_investigation_is_stepped_even_inside_the_300_second_guard(
        safety, fake_hass, clock):
    away(fake_hass)
    motion(fake_hass)
    await safety._check_intrusion(False, False)
    fake_hass.states.set("person.username", "home")                 # residents return, 1 second later
    clock["now"] += 1
    assert await safety._check_intrusion(False, False) is None
    assert safety._investigation is None                            # the step ran and stood it down


@pytest.mark.parametrize("gap,fires", [(299, False), (300, True)])
async def test_a_new_alert_needs_300_seconds_since_the_last_one_started(
        safety, fake_hass, clock, gap, fires):
    away(fake_hass)
    motion(fake_hass)
    assert (await safety._check_intrusion(False, False)) is not None
    safety._investigation = None                                    # it cleared as benign
    clock["now"] += gap
    result = await safety._check_intrusion(False, False)
    assert (result is not None) is fires


async def test_a_learned_benign_ping_still_starts_the_300_second_guard(
        safety, fake_hass, clock, intr, monkeypatch):
    away(fake_hass)
    motion(fake_hass)
    monkeypatch.setattr(intr, "should_damp_weak_alert", lambda area, cam: True)
    assert await safety._check_intrusion(False, False) is None      # damped: silent
    assert safety._investigation is not None                        # but investigating underneath
    assert safety._last_intrusion_alert == clock["now"]


async def test_away_wins_over_confined_and_over_sleeping(safety, fake_hass, clock):
    away(fake_hass)
    motion(fake_hass)
    action = await safety._check_intrusion(False, True, confined=True)
    assert action["type"] == "intrusion_investigating"
    assert safety._investigation["trigger"] == "away"


async def test_confined_while_asleep_falls_through_to_the_sleeping_rules(
        safety, fake_hass, clock):
    """"Confined and awake" is the confined branch. A sleeping, confined house
    that is not away needs a ground floor breach, like any sleeping house."""
    fake_hass.states.set("person.username", "home")
    motion(fake_hass)
    assert await safety._check_intrusion(True, True, confined=True) is None
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    action = await safety._check_intrusion(True, True, confined=True)
    assert action["type"] == "intrusion_investigating"
    assert safety._investigation["trigger"] == "sleeping"


@pytest.mark.parametrize("trigger_state,key", [
    ("away", "intrusion_alert"), ("sleeping", "intrusion_alert_sleep"),
    ("confined", "intrusion_alert_confined")])
async def test_each_trigger_uses_its_own_alert_wording(
        cc, safety, fake_hass, clock, monkeypatch, trigger_state, key):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                         friendly_name="Front Door")
    motion(fake_hass, "binary_sensor.hall_motion")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion",
                         friendly_name="Hall")
    if trigger_state == "away":
        fake_hass.states.set("person.username", "not_home")
        action = await safety._check_intrusion(False, False)
    elif trigger_state == "sleeping":
        fake_hass.states.set("person.username", "home")
        action = await safety._check_intrusion(True, True)
    else:
        fake_hass.states.set("person.username", "home")
        action = await safety._check_intrusion(True, False, confined=True)
    i18n = cc._notify_i18n()
    ctx = i18n.message("intrusion_ctx_open", "en", name="Front Door")
    assert action["message"] == i18n.message(key, "en", honorific="Sir", where="Hall", ctx=ctx)
    assert action["urgency"] == "high" and action["auto_act"] is True
    assert action["entity_id"] == "binary_sensor.hall_motion"


async def test_armed_alarm_without_an_open_entry_says_the_alarm_is_armed(
        cc, fake_hass, clock, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set(ALARM, "armed_away")
    motion(fake_hass)
    action = await safety._check_intrusion(False, False)
    assert action["message"] == cc._notify_i18n().message(
        "intrusion_alert", "en", honorific="Sir", where="binary_sensor.living_motion",
        ctx=" (alarm armed)")


async def test_no_breach_and_no_alarm_leaves_the_context_empty_when_corroboration_is_off(
        cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "intrusion_require_corroboration": False})
    fake_hass.states.set("person.username", "not_home")
    motion(fake_hass)
    action = await safety._check_intrusion(False, False)
    assert "(" not in action["message"]                             # no " (… open)" or " (alarm armed)"
    assert safety._investigation["breach_name"] is None


# ── _begin_investigation ────────────────────────────────────────────────────

async def test_begin_records_the_breach_the_adjacent_rooms_and_the_starting_depth(
        safety, fake_hass, clock, monkeypatch, load):
    _plan(safety, monkeypatch, load, mapping={"binary_sensor.hall_motion": "hall"},
          breach="living", adjacent={"hall", "kitchen"}, hops={"living": 0, "hall": 1, "kitchen": 1})
    safety._begin_investigation(
        now=clock["now"], trigger="away", presence="away", breach="binary_sensor.front",
        breach_name="Front Door", armed=False, eid="binary_sensor.hall_motion",
        where="Hall", honorific="sir", reason="test")
    inv = safety._investigation
    assert inv["connected"] == {"living", "hall", "kitchen"}
    assert inv["zones"] == {"hall"} and inv["path"] == ["hall"]
    assert inv["max_depth"] == 1                                    # started one room in
    assert inv["escalated"] is False and inv["trigger"] == "away"
    assert inv["start"] == inv["last_motion"] == clock["now"]


async def test_begin_without_a_breach_area_connects_nothing(
        safety, fake_hass, clock, monkeypatch, load):
    _plan(safety, monkeypatch, load, breach=None)
    safety._begin_investigation(
        now=clock["now"], trigger="away", presence="away", breach=None, breach_name=None,
        armed=True, eid="binary_sensor.hall_motion", where="Hall", honorific="sir", reason="t")
    assert safety._investigation["connected"] == set()
    assert safety._investigation["breach_area"] is None
    assert safety._investigation["max_depth"] == 0


async def test_begin_survives_a_broken_residence_graph(
        safety, fake_hass, clock, monkeypatch, load):
    monkeypatch.setattr(safety, "_breach_area", lambda e: "living")
    rg = load("residence_graph")

    def boom(*a):
        raise RuntimeError("floor plan unreadable")
    monkeypatch.setattr(rg, "adjacent_areas", boom)
    action = safety._begin_investigation(
        now=clock["now"], trigger="away", presence="away", breach="x", breach_name="X",
        armed=False, eid="binary_sensor.hall_motion", where="Hall", honorific="sir", reason="t")
    assert action["type"] == "intrusion_investigating"             # the alert still goes out
    assert safety._investigation["connected"] == {"living"}
    assert safety._investigation["hops"] == {}


async def test_begin_survives_a_broken_decision_record_and_event_log(
        safety, fake_hass, clock, monkeypatch, load, intr):
    dr = load("decision_record")

    def boom(*a, **k):
        raise RuntimeError("store offline")
    monkeypatch.setattr(dr, "record", boom)
    monkeypatch.setattr(intr, "record_event", boom)
    action = safety._begin_investigation(
        now=clock["now"], trigger="away", presence="away", breach=None, breach_name=None,
        armed=False, eid="binary_sensor.hall_motion", where="Hall", honorific="sir", reason="t")
    assert action["type"] == "intrusion_investigating"
    assert safety._investigation is not None


async def test_begin_logs_investigating_and_a_damped_ping_returns_no_alert(
        safety, fake_hass, clock, monkeypatch, load, intr, records):
    _plan(safety, monkeypatch, load)
    monkeypatch.setattr(intr, "should_damp_weak_alert", lambda area, cam: True)
    out = safety._begin_investigation(
        now=clock["now"], trigger="away", presence="away", breach=None, breach_name="Front",
        armed=False, eid="binary_sensor.hall_motion", where="Hall", honorific="sir",
        reason="the reason")
    assert out is None
    assert records[0][0] == "investigating"
    assert records[0][1]["reason"] == "damped (learned benign)"
    assert safety._investigation is not None                        # the investigation still runs


async def test_begin_links_the_decision_record_for_a_later_call_off(
        safety, fake_hass, clock, monkeypatch, load, intr):
    monkeypatch.setattr(load("decision_record"), "record", lambda *a, **k: 42)
    seen = []
    monkeypatch.setattr(intr, "set_last_decision_id", lambda rid: seen.append(rid))
    safety._begin_investigation(
        now=clock["now"], trigger="away", presence="away", breach=None, breach_name=None,
        armed=False, eid="binary_sensor.hall_motion", where="Hall", honorific="sir", reason="t")
    assert seen == [42]


# ── _investigate_step: whether the situation still holds ────────────────────

@pytest.mark.parametrize("trigger,away_now,sleeping_now,kept", [
    ("away", True, False, True), ("away", False, True, False),
    ("sleeping", False, True, True), ("sleeping", True, False, False),
    ("confined", False, False, True), ("confined", True, True, True),
])
async def test_the_investigation_ends_when_its_own_situation_ends(
        safety, fake_hass, clock, monkeypatch, trigger, away_now, sleeping_now, kept):
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"], trigger=trigger)
    assert await safety._investigate_step(clock["now"], away_now, sleeping_now) is None
    assert (safety._investigation is not None) is kept


async def test_an_old_investigation_without_a_trigger_is_treated_as_away(
        safety, fake_hass, clock, monkeypatch):
    _no_camera(safety, monkeypatch)
    inv = _seed(safety, clock["now"])
    del inv["trigger"]
    await safety._investigate_step(clock["now"], True, False)
    assert safety._investigation is inv
    await safety._investigate_step(clock["now"], False, False)
    assert safety._investigation is None


# ── _investigate_step: tracking motion ──────────────────────────────────────

async def test_motion_extends_the_route_in_order_and_refreshes_last_motion(
        safety, fake_hass, clock, monkeypatch, load):
    _no_camera(safety, monkeypatch)
    _plan(safety, monkeypatch, load, mapping={"binary_sensor.a": "hall", "binary_sensor.b": "study"})
    inv = _seed(safety, clock["now"])
    clock["now"] += 10
    motion(fake_hass, "binary_sensor.a")
    await safety._investigate_step(clock["now"], True, False)
    clock["now"] += 10
    motion(fake_hass, "binary_sensor.a")
    motion(fake_hass, "binary_sensor.b")
    await safety._investigate_step(clock["now"], True, False)
    assert inv["path"][:2] == ["living", "hall"] and inv["path"][-1] == "study"
    assert len(inv["path"]) == 3                                    # hall was not listed twice
    assert inv["zones"] == {"living", "hall", "study"}
    assert inv["last_motion"] == clock["now"]


async def test_depth_only_ever_increases_and_unmapped_rooms_are_ignored(
        safety, fake_hass, clock, monkeypatch, load):
    _no_camera(safety, monkeypatch)
    _plan(safety, monkeypatch, load,
          mapping={"binary_sensor.a": "hall", "binary_sensor.b": "attic", "binary_sensor.c": "shed"})
    inv = _seed(safety, clock["now"], hops={"living": 0, "hall": 1, "attic": 3})
    motion(fake_hass, "binary_sensor.b")
    await safety._investigate_step(clock["now"], True, False)
    assert inv["max_depth"] == 3
    fake_hass.states.set("binary_sensor.b", "off", device_class="motion")
    motion(fake_hass, "binary_sensor.a")
    motion(fake_hass, "binary_sensor.c")                            # no depth for this room
    await safety._investigate_step(clock["now"], True, False)
    assert inv["max_depth"] == 3


# ── _investigate_step: what confirms an intrusion ───────────────────────────

async def test_inward_depth_setting_that_is_not_a_number_falls_back_to_two(
        cc, fake_hass, clock, monkeypatch, load):
    safety = cc.SafetyManager(fake_hass, {"intrusion_inward_depth": "deep"})
    _no_camera(safety, monkeypatch)
    _plan(safety, monkeypatch, load, mapping={"binary_sensor.b": "attic"})
    _seed(safety, clock["now"], hops={"living": 0, "attic": 2}, max_depth=1)
    motion(fake_hass, "binary_sensor.b")
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["type"] == "intrusion_confirmed"                  # depth 2 reached, default need is 2


async def test_without_a_room_map_it_needs_spread_and_motion_near_the_breach(
        safety, fake_hass, clock, monkeypatch, load):
    _no_camera(safety, monkeypatch)
    _plan(safety, monkeypatch, load, mapping={"binary_sensor.b": "hall", "binary_sensor.c": "attic"})
    inv = _seed(safety, clock["now"], hops={}, zones={"cellar"}, path=["cellar"])
    motion(fake_hass, "binary_sensor.c")                            # two zones, but neither near enough
    assert await safety._investigate_step(clock["now"], True, False) is None
    assert inv["escalated"] is False
    fake_hass.states.set("binary_sensor.c", "off", device_class="motion")
    motion(fake_hass, "binary_sensor.b")                            # hall is adjacent to the breach
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["type"] == "intrusion_confirmed"


async def test_the_spread_threshold_is_configurable(
        cc, fake_hass, clock, monkeypatch, load):
    safety = cc.SafetyManager(fake_hass, {"intrusion_spread_zones": 3})
    _no_camera(safety, monkeypatch)
    _plan(safety, monkeypatch, load, mapping={"binary_sensor.b": "hall", "binary_sensor.c": "study"})
    _seed(safety, clock["now"], hops={})
    motion(fake_hass, "binary_sensor.b")
    assert await safety._investigate_step(clock["now"], True, False) is None      # 2 zones < 3
    motion(fake_hass, "binary_sensor.c")
    assert (await safety._investigate_step(clock["now"], True, False))["type"] == "intrusion_confirmed"


async def test_the_confirmed_alert_fires_exactly_once(
        safety, fake_hass, clock, monkeypatch, records):
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"], hops={"living": 0, "hall": 2}, max_depth=2, zones={"living", "hall"})
    first = await safety._investigate_step(clock["now"], True, False)
    again = await safety._investigate_step(clock["now"] + 5, True, False)
    assert first["type"] == "intrusion_confirmed" and again is None
    assert [k for k, _ in records] == ["confirmed"]
    assert safety._investigation["escalated"] is True


async def test_the_confirmed_alert_shape_and_event(safety, fake_hass, clock, monkeypatch, records):
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"], hops={"living": 0, "hall": 2}, max_depth=2,
          zones={"living", "hall"}, breach_name="Front Door")
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["urgency"] == "critical" and action["notify_all"] is True
    assert action["can_dismiss"] is True and action["auto_act"] is True
    assert "intrusion confirmed" in action["message"].lower()
    assert "someone is moving inward" in action["message"]
    assert "snapshot_url" not in action                             # no camera, no snapshot keys
    assert fake_hass.bus.fired == [("nova_intrusion_confirmed", {
        "reason": "someone is moving inward through the house from the point of entry",
        "snapshot_url": None, "snapshot_path": None, "camera": None})]
    kind, kw = records[0]
    assert kind == "confirmed" and kw["zones"] == ["hall", "living"] and kw["max_depth"] == 2
    assert kw["breach"] == "Front Door" and kw["breach_area"] == "living"


async def test_a_failing_event_bus_never_swallows_the_confirmed_alert(
        safety, fake_hass, clock, monkeypatch):
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"], hops={"living": 0, "hall": 2}, max_depth=2)

    def boom(*a, **k):
        raise RuntimeError("bus down")
    monkeypatch.setattr(fake_hass.bus, "async_fire", boom)
    assert (await safety._investigate_step(clock["now"], True, False))["type"] == "intrusion_confirmed"


async def test_a_failing_event_log_never_swallows_the_confirmed_alert(
        safety, fake_hass, clock, monkeypatch, intr):
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"], hops={"living": 0, "hall": 2}, max_depth=2)

    def boom(*a, **k):
        raise RuntimeError("log offline")
    monkeypatch.setattr(intr, "record_event", boom)
    assert (await safety._investigate_step(clock["now"], True, False))["type"] == "intrusion_confirmed"


# ── _investigate_step: camera, vision and snapshots ─────────────────────────

def _camera(safety, monkeypatch, vision, cam="camera.den"):
    _in_breach_room(safety, monkeypatch)
    monkeypatch.setattr(safety, "_person_camera_entity", lambda indoor_only=True: cam)

    async def fake_vision(entity):
        fake_vision.asked.append(entity)
        return vision
    fake_vision.asked = []
    monkeypatch.setattr(safety, "_confirm_person_with_vision", fake_vision)
    return fake_vision


async def test_vision_yes_confirms_without_needing_a_route(
        safety, fake_hass, clock, monkeypatch, intr):
    ask = _camera(safety, monkeypatch, True)

    async def no_snap(hass, cam, tag="intrusion"):
        return None
    monkeypatch.setattr(intr, "capture_snapshot", no_snap)
    _seed(safety, clock["now"])
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["type"] == "intrusion_confirmed"
    assert "a person is on camera (confirmed by vision)" in action["message"]
    assert ask.asked == ["camera.den"]


async def test_vision_no_needs_a_real_route_when_the_breach_is_known(
        safety, fake_hass, clock, monkeypatch):
    _camera(safety, monkeypatch, False)
    inv = _seed(safety, clock["now"], hops={"living": 0, "hall": 2}, max_depth=1)
    assert await safety._investigate_step(clock["now"], True, False) is None
    inv["max_depth"] = 2
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["type"] == "intrusion_confirmed"
    assert "moving inward" in action["message"]


async def test_vision_no_without_a_breach_area_needs_spread_and_sustained_motion(
        safety, fake_hass, clock, monkeypatch, load):
    _camera(safety, monkeypatch, False)
    _plan(safety, monkeypatch, load, mapping={"binary_sensor.b": "hall"})
    _seed(safety, clock["now"], breach_area=None, breach_name=None, connected=set(),
          zones={"living", "hall"})
    motion(fake_hass, "binary_sensor.b")
    assert await safety._investigate_step(clock["now"] + 59, True, False) is None     # not sustained yet
    action = await safety._investigate_step(clock["now"] + 60, True, False)
    assert action["type"] == "intrusion_confirmed"
    assert "sustained movement" in action["message"]


async def test_a_camera_that_covers_the_breach_area_is_preferred_to_any_person_sensor(
        safety, fake_hass, clock, monkeypatch, load):
    ask = _camera(safety, monkeypatch, None, cam="camera.other")
    covers = load("camera_coverage")
    monkeypatch.setattr(covers, "camera_for_area", lambda hass, area: "camera.hall_view")
    _seed(safety, clock["now"])
    await safety._investigate_step(clock["now"], True, False)
    assert ask.asked == ["camera.hall_view"]


async def test_a_broken_coverage_lookup_falls_back_to_the_person_sensor_camera(
        safety, fake_hass, clock, monkeypatch, load):
    ask = _camera(safety, monkeypatch, None, cam="camera.other")
    covers = load("camera_coverage")

    def boom(hass, area):
        raise RuntimeError("floor plan unreadable")
    monkeypatch.setattr(covers, "camera_for_area", boom)
    _seed(safety, clock["now"])
    await safety._investigate_step(clock["now"], True, False)
    assert ask.asked == ["camera.other"]


async def test_the_confirmed_alert_carries_the_snapshot_when_a_camera_can_give_one(
        safety, fake_hass, clock, monkeypatch, intr):
    _camera(safety, monkeypatch, True)

    async def snap(hass, cam, tag="intrusion"):
        return {"path": "/data/snap.jpg", "camera": cam}

    async def url(hass, path):
        return "https://home.example/snap.jpg"
    monkeypatch.setattr(intr, "capture_snapshot", snap)
    monkeypatch.setattr(intr, "get_notification_image_url", url)
    _seed(safety, clock["now"])
    action = await safety._investigate_step(clock["now"], True, False)
    assert "A snapshot is available." in action["message"]
    assert action["snapshot_url"] == "https://home.example/snap.jpg"
    assert action["snapshot_path"] == "/data/snap.jpg" and action["camera"] == "camera.den"
    assert fake_hass.bus.fired[0][1]["snapshot_url"] == "https://home.example/snap.jpg"


async def test_a_failed_snapshot_or_url_does_not_stop_the_alert(
        safety, fake_hass, clock, monkeypatch, intr):
    _camera(safety, monkeypatch, True)

    async def boom(*a, **k):
        raise RuntimeError("camera offline")
    monkeypatch.setattr(intr, "capture_snapshot", boom)
    _seed(safety, clock["now"])
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["type"] == "intrusion_confirmed" and "snapshot_url" not in action

    safety._investigation = None
    _seed(safety, clock["now"])

    async def snap(hass, cam, tag="intrusion"):
        return {"path": "/data/snap.jpg", "camera": cam}
    monkeypatch.setattr(intr, "capture_snapshot", snap)
    monkeypatch.setattr(intr, "get_notification_image_url", boom)
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["snapshot_url"] is None and action["snapshot_path"] == "/data/snap.jpg"


# ── _investigate_step: the call off, acknowledgement and no response timeout ─

async def test_a_call_off_during_the_step_clears_the_investigation(
        safety, fake_hass, clock, monkeypatch, intr):
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"], hops={"living": 0, "hall": 2}, max_depth=2)   # would confirm
    intr.dismiss_intrusion("false alarm")
    assert await safety._investigate_step(clock["now"], True, False) is None
    assert safety._investigation is None


async def test_unanswered_active_motion_sends_one_soft_notice_not_a_confirmation(
        safety, load, fake_hass, clock, monkeypatch, records):
    _one_zone(safety, monkeypatch, load)
    inv = _seed(safety, clock["now"])
    motion(fake_hass)                                               # still moving
    now = clock["now"] + 120                                        # exactly the response window
    action = await safety._investigate_step(now, True, False)
    assert action["type"] == "intrusion_unresolved" and action["urgency"] == "high"
    assert action["notify_all"] is True and action["can_dismiss"] is True
    assert "I flagged possible activity near Front Door" in action["message"]
    assert "have not confirmed" in action["message"]
    assert inv["escalated"] is True
    assert await safety._investigate_step(now + 5, True, False) is None       # and it is not repeated
    assert [k for k, _ in records] == ["unresolved"]
    assert records[0][1]["reason"] == "no response; unconfirmed activity"
    assert fake_hass.bus.fired[0][0] == "nova_intrusion_unresolved"


async def test_the_response_window_is_configurable_and_a_bad_value_means_120(
        cc, fake_hass, clock, monkeypatch):
    for setting, at, fires in ((60, 60, True), (60, 59, False), ("soon", 119, False), ("soon", 120, True)):
        safety = cc.SafetyManager(fake_hass, {"intrusion_response_timeout": setting})
        _no_camera(safety, monkeypatch)
        _seed(safety, clock["now"])
        motion(fake_hass)
        out = await safety._investigate_step(clock["now"] + at, True, False)
        assert (out is not None) is fires, (setting, at)


async def test_a_quiet_house_gets_no_soft_notice_because_it_clears_as_benign(
        safety, fake_hass, clock, monkeypatch):
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"])
    # No motion for 181 seconds: nothing is "still active", so no timeout, so it clears.
    assert await safety._investigate_step(clock["now"] + 181, True, False) is None
    assert safety._investigation is None


async def test_an_acknowledgement_holds_the_soft_notice(
        safety, load, fake_hass, clock, monkeypatch, intr):
    _one_zone(safety, monkeypatch, load)
    _seed(safety, clock["now"])
    motion(fake_hass)
    intr.acknowledge("looking now")
    assert await safety._investigate_step(clock["now"] + 200, True, False) is None
    assert safety._investigation["escalated"] is False


async def test_a_damped_soft_notice_is_silent_but_marks_the_investigation_done(
        safety, load, fake_hass, clock, monkeypatch, intr, records):
    _one_zone(safety, monkeypatch, load)
    inv = _seed(safety, clock["now"])
    motion(fake_hass)
    monkeypatch.setattr(intr, "should_damp_weak_alert", lambda area, cam: True)
    assert await safety._investigate_step(clock["now"] + 130, True, False) is None
    assert inv["escalated"] is True                                 # marked done even though silent
    assert records[-1][1]["reason"] == "damped (learned benign)"
    assert fake_hass.bus.fired == []


async def test_the_soft_notice_includes_a_snapshot_when_the_camera_is_known_but_unconfirmed(
        safety, fake_hass, clock, monkeypatch, intr):
    """Vision says no person and there is no inward route, so nothing is
    confirmed; the unanswered window still produces the soft notice, with the
    snapshot attached."""
    _camera(safety, monkeypatch, False)

    async def snap(hass, cam, tag="intrusion"):
        return {"path": "/data/s.jpg", "camera": cam}

    async def url(hass, path):
        return "https://home.example/s.jpg"
    monkeypatch.setattr(intr, "capture_snapshot", snap)
    monkeypatch.setattr(intr, "get_notification_image_url", url)
    _seed(safety, clock["now"])
    motion(fake_hass)
    action = await safety._investigate_step(clock["now"] + 130, True, False)
    assert action["type"] == "intrusion_unresolved"
    assert "A snapshot is available." in action["message"]
    assert action["snapshot_url"] == "https://home.example/s.jpg"
    assert fake_hass.bus.fired[0][1]["camera"] == "camera.den"


async def test_a_failed_snapshot_does_not_stop_the_soft_notice(
        safety, fake_hass, clock, monkeypatch, intr):
    _camera(safety, monkeypatch, False)

    async def boom(*a, **k):
        raise RuntimeError("camera offline")
    monkeypatch.setattr(intr, "capture_snapshot", boom)
    _seed(safety, clock["now"])
    motion(fake_hass)
    action = await safety._investigate_step(clock["now"] + 130, True, False)
    assert action["type"] == "intrusion_unresolved" and "snapshot_url" not in action


async def test_the_soft_notice_names_the_point_of_entry_even_when_it_is_unknown(
        safety, load, fake_hass, clock, monkeypatch):
    _one_zone(safety, monkeypatch, load)
    _seed(safety, clock["now"], breach_name=None, breach_area=None, connected=set())
    motion(fake_hass)
    action = await safety._investigate_step(clock["now"] + 130, True, False)
    assert "near the point of entry" in action["message"]


async def test_current_behaviour_a_real_route_after_the_soft_notice_is_never_escalated(
        safety, fake_hass, clock, monkeypatch):
    """After the "couldn't reach you" notice the investigation is marked
    escalated, and the confirmed alert only fires while it is not. Someone who
    then walks inward through the house stays at the soft notice."""
    _no_camera(safety, monkeypatch)
    inv = _seed(safety, clock["now"], hops={"living": 0, "hall": 2})
    motion(fake_hass)
    assert (await safety._investigate_step(clock["now"] + 130, True, False))["type"] == "intrusion_unresolved"
    inv["max_depth"] = 2                                            # the route is now real
    assert await safety._investigate_step(clock["now"] + 140, True, False) is None


async def test_current_behaviour_a_damped_soft_notice_also_blocks_a_later_confirmation(
        safety, fake_hass, clock, monkeypatch, intr):
    _no_camera(safety, monkeypatch)
    inv = _seed(safety, clock["now"], hops={"living": 0, "hall": 2})
    motion(fake_hass)
    monkeypatch.setattr(intr, "should_damp_weak_alert", lambda area, cam: True)
    assert await safety._investigate_step(clock["now"] + 130, True, False) is None
    inv["max_depth"] = 2
    assert await safety._investigate_step(clock["now"] + 140, True, False) is None   # silent confirmation


# ── _investigate_step: clearing ─────────────────────────────────────────────

async def test_quiet_time_clears_an_unescalated_investigation_only_past_180_seconds(
        safety, fake_hass, clock, monkeypatch, intr):
    _no_camera(safety, monkeypatch)
    intr.acknowledge("holding")                                     # keep the soft notice out of the way
    _seed(safety, clock["now"])
    await safety._investigate_step(clock["now"] + 180, True, False)
    assert safety._investigation is not None                        # 180 is not "more than 180"
    await safety._investigate_step(clock["now"] + 181, True, False)
    assert safety._investigation is None


async def test_ten_minutes_with_one_zone_clears_it_when_the_user_is_holding_the_notice(
        safety, load, fake_hass, clock, monkeypatch, intr):
    _one_zone(safety, monkeypatch, load)
    _seed(safety, clock["now"])
    motion(fake_hass)
    intr.acknowledge("holding")
    await safety._investigate_step(clock["now"] + 600, True, False)
    assert safety._investigation is not None
    await safety._investigate_step(clock["now"] + 601, True, False)
    assert safety._investigation is None


async def test_current_behaviour_a_response_window_over_ten_minutes_never_sends_the_soft_notice(
        cc, fake_hass, clock, monkeypatch):
    safety = cc.SafetyManager(fake_hass, {"intrusion_response_timeout": 900})
    _no_camera(safety, monkeypatch)
    _seed(safety, clock["now"])
    motion(fake_hass)
    assert await safety._investigate_step(clock["now"] + 601, True, False) is None
    assert safety._investigation is None                            # cleared as benign at ten minutes


async def test_an_escalated_investigation_clears_once_the_house_settles(
        safety, fake_hass, clock, monkeypatch):
    _no_camera(safety, monkeypatch)
    inv = _seed(safety, clock["now"], escalated=True)
    await safety._investigate_step(clock["now"] + 180, True, False)
    assert safety._investigation is inv                             # not yet
    await safety._investigate_step(clock["now"] + 181, True, False)
    assert safety._investigation is None


async def test_an_escalated_investigation_stays_open_while_there_is_still_motion(
        safety, fake_hass, clock, monkeypatch):
    _no_camera(safety, monkeypatch)
    inv = _seed(safety, clock["now"], escalated=True)
    motion(fake_hass)
    await safety._investigate_step(clock["now"] + 5000, True, False)
    assert safety._investigation is inv


# ── _confirm_person_with_vision ─────────────────────────────────────────────

@pytest.fixture
def vision_env(safety, monkeypatch):
    """A stand in for camera.py (which needs aiohttp) with a scripted vision
    reply, recording how the probe was called."""
    calls = []
    reply = {"value": None, "exc": None}

    class FakeCall:
        def __init__(self, data):
            self.data = data

    async def analyze(hass, call, client, honorific, p1, p2, gate_announce=False):
        calls.append({"data": call.data, "client": client, "gate": gate_announce})
        if reply["exc"] is not None:
            raise reply["exc"]
        return reply["value"]
    stand_in = types.ModuleType("jc.camera")
    stand_in.async_analyze_camera = analyze
    stand_in._FakeCall = FakeCall
    monkeypatch.setitem(sys.modules, "jc.camera", stand_in)
    return types.SimpleNamespace(calls=calls, reply=reply, safety=safety)


@pytest.mark.parametrize("text,expected", [
    ("PERSON: YES", True),
    ("person: no", False),
    ("PERSON: NO. The room is empty.", False),                      # the explicit signal wins over the words
    ("There is no person in the frame", False),
    ("The room looks empty", False),
    ("A man is standing by the window", True),
    ("someone is in the hallway", True),
    ("An empty room, but someone is there", None),                  # both signals: ambiguous
    ("Cannot tell. Dark and blurry.", None),
    ("", None),
])
async def test_vision_reply_text_is_read_conservatively(vision_env, text, expected):
    vision_env.reply["value"] = {"analysis": text}
    assert await vision_env.safety._confirm_person_with_vision("camera.den") is expected


async def test_vision_reads_analysis_then_description_then_message(vision_env):
    for key in ("analysis", "description", "message"):
        vision_env.reply["value"] = {key: "PERSON: YES"}
        assert await vision_env.safety._confirm_person_with_vision("camera.den") is True
    vision_env.reply["value"] = {"analysis": "", "description": "person: no"}
    assert await vision_env.safety._confirm_person_with_vision("camera.den") is False


async def test_vision_is_inconclusive_for_a_non_dict_reply_or_an_error(vision_env):
    vision_env.reply["value"] = "PERSON: YES"                       # not a dict
    assert await vision_env.safety._confirm_person_with_vision("camera.den") is None
    vision_env.reply["exc"] = RuntimeError("model down")
    assert await vision_env.safety._confirm_person_with_vision("camera.den") is None   # never suppresses


async def test_the_vision_probe_stays_out_of_scene_memory_and_is_not_announced(vision_env):
    vision_env.reply["value"] = {"analysis": "PERSON: NO"}
    vision_env.safety.groq_client = "client-1"
    await vision_env.safety._confirm_person_with_vision("camera.den")
    call = vision_env.calls[0]
    assert call["data"]["entity_id"] == "camera.den"
    assert call["data"]["announce"] is False and call["data"]["record_scene"] is False
    assert call["gate"] is True and call["client"] == "client-1"
    assert "PERSON: YES" in call["data"]["prompt"]


async def test_the_vision_kill_switch_means_no_second_opinion(vision_env, cc, fake_hass):
    vision_env.safety.config["intrusion_vision_confirm"] = False
    assert await vision_env.safety._confirm_person_with_vision("camera.den") is None
    assert vision_env.calls == []


# ── intrusion_status ────────────────────────────────────────────────────────

def test_intrusion_status_reports_nothing_without_a_safety_manager(cc):
    assert cc.intrusion_status() == {"active": False, "confirmed": False}


def test_intrusion_status_reports_the_route_and_whether_it_was_confirmed(cc, safety):
    cc._CORE.safety_mgr = safety
    assert cc.intrusion_status() == {"active": False, "confirmed": False}
    _seed(safety, 1.0, zones={"living", "hall"}, path=["living", "hall"], escalated=True)
    status = cc.intrusion_status()
    assert status == {"active": True, "confirmed": True, "breach_area": "living",
                      "breach_name": "Front Door", "path": ["living", "hall"],
                      "zones": ["hall", "living"]}


# ── an unreadable helper never blocks or hides an alert ─────────────────────

def _boom(*a, **k):
    raise RuntimeError("helper broke")


async def test_a_broken_call_off_check_does_not_stop_a_new_investigation(
        safety, fake_hass, clock, intr, monkeypatch):
    monkeypatch.setattr(intr, "is_called_off", _boom)
    away(fake_hass)
    motion(fake_hass)
    assert (await safety._check_intrusion(False, False))["type"] == "intrusion_investigating"


async def test_a_broken_call_off_check_inside_the_step_does_not_clear_or_block_the_alert(
        safety, fake_hass, clock, intr, monkeypatch):
    _no_camera(safety, monkeypatch)
    monkeypatch.setattr(intr, "is_called_off", _boom)
    _seed(safety, clock["now"], hops={"living": 0, "hall": 2}, max_depth=2)
    assert (await safety._investigate_step(clock["now"], True, False))["type"] == "intrusion_confirmed"


async def test_a_broken_acknowledgement_check_counts_as_not_acknowledged(
        safety, fake_hass, clock, intr, monkeypatch):
    _no_camera(safety, monkeypatch)
    monkeypatch.setattr(intr, "is_acknowledged", _boom)
    _seed(safety, clock["now"])
    motion(fake_hass)
    action = await safety._investigate_step(clock["now"] + 130, True, False)
    assert action["type"] == "intrusion_unresolved"


async def test_a_broken_damping_lookup_means_the_soft_notice_is_sent(
        safety, fake_hass, clock, intr, monkeypatch):
    _no_camera(safety, monkeypatch)
    monkeypatch.setattr(intr, "should_damp_weak_alert", _boom)
    _seed(safety, clock["now"])
    motion(fake_hass)
    assert (await safety._investigate_step(clock["now"] + 130, True, False))["type"] == "intrusion_unresolved"


async def test_a_broken_event_log_bus_or_image_url_never_swallows_the_soft_notice(
        safety, fake_hass, clock, intr, monkeypatch):
    _camera(safety, monkeypatch, False)

    async def snap(hass, cam, tag="intrusion"):
        return {"path": "/data/s.jpg", "camera": cam}

    async def bad_url(hass, path):
        raise RuntimeError("no url")
    monkeypatch.setattr(intr, "capture_snapshot", snap)
    monkeypatch.setattr(intr, "get_notification_image_url", bad_url)
    monkeypatch.setattr(intr, "record_event", _boom)
    monkeypatch.setattr(fake_hass.bus, "async_fire", _boom)
    _seed(safety, clock["now"])
    motion(fake_hass)
    action = await safety._investigate_step(clock["now"] + 130, True, False)
    assert action["type"] == "intrusion_unresolved"
    assert action["snapshot_url"] is None and action["snapshot_path"] == "/data/s.jpg"


async def test_a_vision_timeout_is_inconclusive_so_the_camera_is_trusted_and_it_confirms(
        safety, fake_hass, clock, monkeypatch, intr, vision_env):
    """The second opinion never suppresses a real alert: if the vision model
    times out or errors, the investigation falls back to trusting the camera."""
    vision_env.reply["exc"] = TimeoutError("vision model too slow")
    monkeypatch.setattr(safety, "_person_camera_entity", lambda indoor_only=True: "camera.den")

    async def no_snap(hass, cam, tag="intrusion"):
        return None
    monkeypatch.setattr(intr, "capture_snapshot", no_snap)
    _seed(safety, clock["now"])
    action = await safety._investigate_step(clock["now"], True, False)
    assert action["type"] == "intrusion_confirmed"
    assert "a person is on camera" in action["message"] and "confirmed by vision" not in action["message"]
