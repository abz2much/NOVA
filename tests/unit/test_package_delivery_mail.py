"""Delivery and mail improvements to package_monitor (8.7.6):

  1. watched_cameras matches whole word tokens, so wide views such as
     camera.front_yard are no longer swept.
  2. A 30 minute speech cooldown per (camera, kind). Only speech is gated.
  3. Porch motion and mailbox sensors can bring a check forward.
"""
import sys
import types

import pytest

CTX = lambda: ("Sir", "tts.x", ["media_player.y"])  # noqa: E731


@pytest.fixture
def pm(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    mod = load("package_monitor")
    for d in (mod._STATE, mod._LAST_SPOKEN, mod._TRIGGER_LAST):
        d.clear()
    yield mod
    for d in (mod._STATE, mod._LAST_SPOKEN, mod._TRIGGER_LAST):
        d.clear()


@pytest.fixture
def clock(pm, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(pm, "_now_mono", lambda: t["now"])
    return t


@pytest.fixture
def env(pm, load, monkeypatch):
    """Announcements on, no quiet hours, package watch on. Returns the dict of
    switches and a list that collects every spoken message."""
    sw = {"quiet": False, "announce": True, "detect": True}
    monkeypatch.setattr(pm, "_in_quiet_hours", lambda h: sw["quiet"])
    monkeypatch.setattr(pm, "_announcements_on", lambda h: sw["announce"])
    monkeypatch.setattr(pm, "_package_detection_on", lambda h: sw["detect"])
    spoken: list[str] = []

    async def _announce(hass, msg, *a, **k):
        spoken.append(msg)
    monkeypatch.setattr(load("tts_helper"), "async_announce", _announce)
    sw["spoken"] = spoken
    logs: list[tuple] = []
    monkeypatch.setattr(pm, "_log", lambda hass, eid, kind, det, source:
                        logs.append((eid, kind, source)))
    sw["logs"] = logs
    return sw


def _det(package=False, mail=False, count=None):
    return {"package": package, "mail": mail,
            "count": (1 if package else 0) if count is None else count,
            "description": ""}


async def _eval(pm, hass, eid, det, source="periodic"):
    return await pm.evaluate(hass, None, "Sir", "tts.x", ["media_player.y"],
                             eid, det, source=source)


# ── 1. Camera name matching ──────────────────────────────────────────────────

@pytest.fixture
def cams(pm, load, fake_hass, monkeypatch):
    # camera.py needs aiohttp. An earlier test in the same process may have
    # left a half loaded copy in the module cache; start from a clean one.
    stale = sys.modules.get("jc.camera")
    if stale is not None and not hasattr(stale, "active_camera_states"):
        monkeypatch.delitem(sys.modules, "jc.camera")
    load("camera")
    eufy_mod = load("eufy")
    monkeypatch.setattr(eufy_mod, "is_eufy_camera", lambda h, e: False)
    monkeypatch.setattr(eufy_mod, "discover_roles", lambda h, e: {})
    return fake_hass


@pytest.mark.parametrize("name", [
    "front_door", "frontdoor", "front_door_hd", "porch", "doorbell",
    "front", "frontdoor_hd", "front_porch_wide", "frontporch", "frontdoorbell",
    "back_porch", "front_door_2", "frontdoor2", "my_doorbell",
])
def test_front_door_and_porch_names_are_watched(pm, cams, name):
    cams.states.set(f"camera.{name}", "idle")
    assert pm.watched_cameras(cams) == [f"camera.{name}"]


@pytest.mark.parametrize("name", [
    "front_yard", "front_garden", "front_driveway", "backyard", "garage",
    "frontyard", "front_garage_door", "porch_driveway", "front_lawn",
    "street", "front_gate", "porch_patio", "hallway", "kitchen",
])
def test_wide_views_and_other_rooms_are_not_watched(pm, cams, name):
    cams.states.set(f"camera.{name}", "idle")
    assert pm.watched_cameras(cams) == []


def test_configured_camera_bypasses_the_name_filter(pm, cams):
    cams.states.set("camera.front_yard", "idle")
    cams.states.set("camera.garage", "idle")
    assert pm.watched_cameras(cams, "camera.front_yard") == ["camera.front_yard"]
    assert pm.watched_cameras(cams, ["camera.front_yard", "camera.garage",
                                     "camera.missing"]) == [
        "camera.front_yard", "camera.garage"]


def test_name_filter_only_looks_at_the_object_part(pm):
    # The domain is never part of the match.
    assert pm._is_porch_view("camera.front_door") is True
    assert pm._is_porch_view("front_door") is True
    assert pm._is_porch_view("porch.kitchen") is False


# ── 2. Announce cooldown ─────────────────────────────────────────────────────

async def test_second_delivery_inside_30_minutes_is_silent_but_recorded(
        pm, fake_hass, env, clock):
    cam = "camera.front_door"
    assert await _eval(pm, fake_hass, cam, _det(package=True)) is True
    assert len(env["spoken"]) == 1
    clock["now"] += 300
    await _eval(pm, fake_hass, cam, _det(package=False))          # picked up
    clock["now"] += 300
    spoke = await _eval(pm, fake_hass, cam, _det(package=True, count=2))
    assert spoke is False and len(env["spoken"]) == 1             # not spoken
    # ...but logged and the state is up to date.
    assert [k for _, k, _ in env["logs"]].count("delivered") == 2
    assert pm._STATE[cam]["package"] is True and pm._STATE[cam]["count"] == 2


async def test_delivery_after_30_minutes_speaks_again(pm, fake_hass, env, clock):
    cam = "camera.front_door"
    await _eval(pm, fake_hass, cam, _det(package=True))
    await _eval(pm, fake_hass, cam, _det(package=False))
    clock["now"] += 1799
    await _eval(pm, fake_hass, cam, _det(package=True))
    assert len(env["spoken"]) == 1
    await _eval(pm, fake_hass, cam, _det(package=False))
    clock["now"] += 2                                             # 1801 s since the first
    await _eval(pm, fake_hass, cam, _det(package=True))
    assert len(env["spoken"]) == 2


async def test_removed_while_away_is_not_held_back_by_a_delivered_cooldown(
        pm, fake_hass, env, clock):
    cam = "camera.front_door"
    await _eval(pm, fake_hass, cam, _det(package=True))           # delivered, spoken
    fake_hass.states.set("person.alex", "not_home")
    clock["now"] += 60
    await _eval(pm, fake_hass, cam, _det(package=False))          # removed while away
    assert len(env["spoken"]) == 2 and "no one is home" in env["spoken"][1]


async def test_a_repeat_removed_alert_is_gated_on_its_own_kind(
        pm, fake_hass, env, clock):
    cam = "camera.front_door"
    fake_hass.states.set("person.alex", "not_home")
    await _eval(pm, fake_hass, cam, _det(package=True))
    await _eval(pm, fake_hass, cam, _det(package=False))          # removed, spoken
    clock["now"] += 100
    await _eval(pm, fake_hass, cam, _det(package=True))           # delivered, silent
    await _eval(pm, fake_hass, cam, _det(package=False))          # removed, silent
    assert len(env["spoken"]) == 2                                # 1 delivered + 1 removed
    assert [k for _, k, _ in env["logs"]].count("removed") == 2


async def test_mail_is_gated_separately_from_packages(pm, fake_hass, env, clock):
    cam = "camera.front_door"
    await _eval(pm, fake_hass, cam, _det(mail=True))
    await _eval(pm, fake_hass, cam, _det(mail=False))
    clock["now"] += 60
    await _eval(pm, fake_hass, cam, _det(mail=True))              # silent repeat
    assert len([m for m in env["spoken"] if "mail" in m]) == 1
    await _eval(pm, fake_hass, cam, _det(package=True))           # still speaks
    assert any("package has been delivered" in m for m in env["spoken"])


async def test_cooldown_is_per_camera(pm, fake_hass, env, clock):
    await _eval(pm, fake_hass, "camera.front_door", _det(package=True))
    await _eval(pm, fake_hass, "camera.porch", _det(package=True))
    assert len(env["spoken"]) == 2


async def test_unspoken_events_do_not_start_the_cooldown(pm, fake_hass, env, clock):
    cam = "camera.front_door"
    env["quiet"] = True
    await _eval(pm, fake_hass, cam, _det(package=True))
    assert env["spoken"] == [] and pm._LAST_SPOKEN == {}
    await _eval(pm, fake_hass, cam, _det(package=False))
    env["quiet"] = False
    await _eval(pm, fake_hass, cam, _det(package=True))
    assert len(env["spoken"]) == 1


async def test_stranded_is_gated_but_still_logged(pm, fake_hass, env, clock):
    for _ in range(2):
        await pm.note_from_eufy(fake_hass, "Sir", "tts.x", ["media_player.y"],
                                "camera.front_door", "package_stranded")
    assert len(env["spoken"]) == 1
    assert [k for _, k, _ in env["logs"]] == ["stranded", "stranded"]
    clock["now"] += 1800
    await pm.note_from_eufy(fake_hass, "Sir", "tts.x", ["media_player.y"],
                            "camera.front_door", "package_stranded")
    assert len(env["spoken"]) == 2


async def test_stranded_does_not_share_a_cooldown_with_delivered(
        pm, fake_hass, env, clock):
    await pm.note_from_eufy(fake_hass, "Sir", "tts.x", ["media_player.y"],
                            "camera.front_door", "package_delivered")
    await pm.note_from_eufy(fake_hass, "Sir", "tts.x", ["media_player.y"],
                            "camera.front_door", "package_stranded")
    assert len(env["spoken"]) == 2


def test_cooldown_state_lives_next_to_state_with_no_module_level_calls(pm):
    import ast
    import pathlib
    src = pathlib.Path(pm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") in (
                "_STATE", "_LAST_SPOKEN", "_TRIGGER_LAST"):
            assert isinstance(node.value, ast.Dict), ast.unparse(node)
    assert "global " not in src


# ── 3a. Sensor discovery ─────────────────────────────────────────────────────

@pytest.fixture
def sensors(pm, fake_hass, monkeypatch):
    monkeypatch.setattr(pm, "_eufy_covered_entities", lambda h: set())

    def add(name, dc=None):
        attrs = {"device_class": dc} if dc else {}
        fake_hass.states.set(f"binary_sensor.{name}", "off", **attrs)
    fake_hass.add = add
    return fake_hass


@pytest.mark.parametrize("name,dc", [
    ("front_door_motion", "motion"), ("porch_occupancy", "occupancy"),
    ("doorbell_presence", "presence"), ("frontdoor_pir", "motion"),
    ("front_porch_wide_motion", "motion"),
])
def test_porch_motion_sensors_are_discovered(pm, sensors, name, dc):
    sensors.add(name, dc)
    assert pm.discover_trigger_sensors(sensors) == {f"binary_sensor.{name}": "motion"}


@pytest.mark.parametrize("name,dc", [
    ("front_door_motion", "door"),         # wrong device class
    ("front_door_motion", None),           # no device class
    ("front_door_contact", "opening"),
    ("front_yard_motion", "motion"),       # wide view
    ("front_driveway_motion", "motion"),
    ("garage_motion", "motion"),
    ("backyard_motion", "motion"),
    ("hallway_motion", "motion"),          # not a porch name
])
def test_other_sensors_are_ignored(pm, sensors, name, dc):
    sensors.add(name, dc)
    assert pm.discover_trigger_sensors(sensors) == {}


@pytest.mark.parametrize("name,dc", [
    ("mailbox", None), ("mailbox_door", "door"), ("letterbox", "opening"),
    ("postbox_contact", "occupancy"), ("front_mailbox", "opening"),
])
def test_mailbox_sensors_are_discovered(pm, sensors, name, dc):
    sensors.add(name, dc)
    assert pm.discover_trigger_sensors(sensors) == {f"binary_sensor.{name}": "mailbox"}


@pytest.mark.parametrize("name,dc", [
    ("mailbox", "motion"), ("mailbox_battery", "battery"),
    ("mail_box", "door"), ("mailboxes", "door"),
])
def test_other_mailbox_names_and_classes_are_ignored(pm, sensors, name, dc):
    sensors.add(name, dc)
    assert pm.discover_trigger_sensors(sensors) == {}


def test_eufy_covered_sensors_are_not_discovered(pm, fake_hass, monkeypatch):
    fake_hass.states.set("binary_sensor.front_door_cam_motion", "off", device_class="motion")
    fake_hass.states.set("binary_sensor.porch_motion", "off", device_class="motion")
    monkeypatch.setattr(pm, "_eufy_covered_entities",
                        lambda h: {"binary_sensor.front_door_cam_motion"})
    assert pm.discover_trigger_sensors(fake_hass) == {
        "binary_sensor.porch_motion": "motion"}


def test_eufy_coverage_follows_roles_and_device(pm, load, fake_hass, monkeypatch):
    eufy_mod = load("eufy")
    monkeypatch.setattr(eufy_mod, "all_camera_roles", lambda h: {
        "camera.front_door_cam": {"package_delivered": "binary_sensor.pkg",
                                  "person": "binary_sensor.person"},
        "camera.porch_cam": {"person": "binary_sensor.porch_person"},   # no package role
    })
    ents = {
        "a": types.SimpleNamespace(entity_id="binary_sensor.front_door_cam_motion",
                                   device_id="dev1"),
        "b": types.SimpleNamespace(entity_id="binary_sensor.other_motion",
                                   device_id="dev2"),
    }
    reg = types.SimpleNamespace(
        entities=ents,
        async_get=lambda eid: types.SimpleNamespace(device_id="dev1"))
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: reg)
    covered = pm._eufy_covered_entities(fake_hass)
    assert covered == {"binary_sensor.pkg", "binary_sensor.person",
                       "binary_sensor.front_door_cam_motion"}


# ── 3b. Motion trigger ───────────────────────────────────────────────────────

@pytest.fixture
def sweep(pm, monkeypatch):
    """Record sleeps and periodic_check calls, in order."""
    calls: list = []

    async def _sleep(seconds):
        calls.append(("sleep", seconds))

    async def _check(hass, client, honorific, tts, speakers, configured_camera=None):
        calls.append(("check", honorific, configured_camera))
        return {"checked": 1, "cameras": []}

    monkeypatch.setattr(pm.asyncio, "sleep", _sleep)
    monkeypatch.setattr(pm, "periodic_check", _check)
    return calls


async def test_motion_waits_20_seconds_then_sweeps_once(pm, fake_hass, env, clock, sweep):
    out = await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion")
    assert out == {"checked": 1, "cameras": []}
    assert sweep == [("sleep", 20.0), ("check", "Sir", None)]


async def test_motion_is_debounced_to_one_early_check_per_3_minutes(
        pm, fake_hass, env, clock, sweep):
    await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion")
    clock["now"] += 179
    assert await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion") is None
    # The 3 minutes are in total, across sensors.
    assert await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.doorbell_motion") is None
    assert sum(1 for c in sweep if c[0] == "check") == 1
    clock["now"] += 1
    assert await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion") is not None
    assert sum(1 for c in sweep if c[0] == "check") == 2


@pytest.mark.parametrize("switch,value", [
    ("quiet", True), ("detect", False), ("announce", False)])
async def test_motion_does_nothing_in_quiet_hours_or_with_the_flag_or_announcements_off(
        pm, fake_hass, env, clock, sweep, switch, value):
    env[switch] = value
    assert await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion") is None
    assert sweep == [] and pm._TRIGGER_LAST == {}                 # no wait, no sweep, no stamp
    env[switch] = not value
    assert await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion") is not None


async def test_motion_stops_if_the_flag_goes_off_during_the_wait(
        pm, fake_hass, env, clock, sweep, monkeypatch):
    async def _sleep(seconds):
        env["detect"] = False
    monkeypatch.setattr(pm.asyncio, "sleep", _sleep)
    assert await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion") is None
    assert [c for c in sweep if c[0] == "check"] == []


async def test_motion_sweep_keeps_the_existing_second_look_and_cooldown(
        pm, load, cams, env, clock, monkeypatch):
    """The early check is periodic_check itself: a new positive still needs a
    second frame, and speech still goes through the 30 minute cooldown."""
    async def _sleep(seconds):
        pass
    monkeypatch.setattr(pm.asyncio, "sleep", _sleep)
    fake_hass = cams
    fake_hass.states.set("camera.front_door", "idle")
    frames = []

    async def _detect(hass, client, eid):
        frames.append(eid)
        return _det(package=True)
    monkeypatch.setattr(pm, "detect_on_camera", _detect)
    await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion")
    assert len(frames) == 2                                       # first look + second look
    assert len(env["spoken"]) == 1
    # A package that is already known is not announced again.
    clock["now"] += 200
    await pm.note_from_motion(fake_hass, None, CTX, "binary_sensor.porch_motion")
    assert len(env["spoken"]) == 1 and len(frames) == 3


# ── 3c. Mailbox trigger ──────────────────────────────────────────────────────

BOX = "binary_sensor.mailbox"


async def test_mailbox_announces_once(pm, fake_hass, env, clock):
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is True
    assert len(env["spoken"]) == 1 and "mail has arrived" in env["spoken"][0]
    assert env["logs"] == [(BOX, "mail", "mailbox")]              # source is "mailbox"
    assert pm._STATE[BOX]["mail"] is True and pm._STATE[BOX]["package"] is False
    # Opened again after the 5 minute debounce: mail is already True, so nothing.
    clock["now"] += 301
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is False
    assert len(env["spoken"]) == 1


async def test_mailbox_is_ignored_when_mail_is_already_true(pm, fake_hass, env, clock):
    pm._STATE[BOX] = {"package": False, "mail": True, "count": 0}
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is False
    assert env["spoken"] == [] and env["logs"] == []
    assert pm._STATE[BOX] == {"package": False, "mail": True, "count": 0}


async def test_mailbox_mail_flag_from_an_earlier_delivery_does_not_block_a_new_one(
        pm, fake_hass, env, clock):
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
    pm._STATE[BOX] = {"package": False, "mail": True, "count": 0, "since": old}
    pm._LAST_SPOKEN[(BOX, "mail")] = clock["now"] - 5 * 3600
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is True
    assert len(env["spoken"]) == 1


async def test_mailbox_has_a_5_minute_debounce_per_sensor(pm, fake_hass, env, clock):
    await pm.note_from_mailbox(fake_hass, None, CTX, BOX)
    pm._STATE.clear()
    pm._LAST_SPOKEN.clear()                                       # isolate the debounce
    clock["now"] += 299
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is False
    assert BOX not in pm._STATE                                   # not even evaluated
    # A different sensor is not held back by this one.
    assert await pm.note_from_mailbox(fake_hass, None, CTX, "binary_sensor.letterbox") is True
    clock["now"] += 2
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is True


async def test_mailbox_uses_the_30_minute_cooldown(pm, fake_hass, env, clock):
    await pm.note_from_mailbox(fake_hass, None, CTX, BOX)
    pm._STATE.clear()                                             # the mail was collected
    clock["now"] += 600
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is False
    assert len(env["spoken"]) == 1
    assert pm._STATE[BOX]["mail"] is True                         # recorded, not spoken
    assert [k for _, k, _ in env["logs"]] == ["mail", "mail"]
    pm._STATE.clear()
    clock["now"] += 1300                                          # 1900 s after the first
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is True
    assert len(env["spoken"]) == 2


@pytest.mark.parametrize("switch,value", [("quiet", True), ("announce", False)])
async def test_mailbox_is_silent_in_quiet_hours_and_with_announcements_off(
        pm, fake_hass, env, clock, switch, value):
    env[switch] = value
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is False
    assert env["spoken"] == [] and pm._LAST_SPOKEN == {}
    assert pm._STATE[BOX]["mail"] is True                         # state machine as usual


async def test_mailbox_does_nothing_with_the_package_flag_off(pm, fake_hass, env, clock):
    env["detect"] = False
    assert await pm.note_from_mailbox(fake_hass, None, CTX, BOX) is False
    assert env["spoken"] == [] and env["logs"] == [] and pm._STATE == {}
    assert pm._TRIGGER_LAST == {}


async def test_mailbox_leaves_the_package_state_unchanged(pm, fake_hass, env, clock):
    pm._STATE[BOX] = {"package": True, "mail": False, "count": 2}
    await pm.note_from_mailbox(fake_hass, None, CTX, BOX)
    assert pm._STATE[BOX]["package"] is True and pm._STATE[BOX]["count"] == 2
    assert len(env["spoken"]) == 1 and "mail has arrived" in env["spoken"][0]
    assert [k for _, k, _ in env["logs"]] == ["mail"]             # no delivered / removed
