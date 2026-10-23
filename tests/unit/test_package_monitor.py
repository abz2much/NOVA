"""package_monitor: keeping false deliveries and false thefts off the speakers.

Covers reading a doorbell description with negations taken into account,
the vision check (blank frames, garbled model replies, temperature 0), the
second look a new arrival or an away pickup needs before anything is said,
Eufy's own package sensors, and doorbell presses that mention post or
nobody's parcel at all. Cooldowns, sensor triggers and the mailbox live in
test_package_delivery_mail.py.
"""
import contextlib
import sys
import types

import pytest

from conftest import route_delivery

DOOR = "camera.front_door_test"
BELL = "camera.front_door_bell"
SPEAKERS = ["media_player.y"]


def _seen(package=False, mail=False, count=None, description=""):
    """A detection dict as the vision check returns it."""
    if count is None:
        count = 1 if package else 0
    return {"package": package, "mail": mail, "count": count, "description": description}


@pytest.fixture(autouse=True)
def _house(load, monkeypatch):
    """Every test starts with no tracked cameras, a stand-in aiohttp (camera
    imports it, and nothing else may have provided one yet), and package
    messages sent through delivery.py under the monitor's own quiet hours
    and Announcements switch."""
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    monitor = load("package_monitor")
    route_delivery(load, monkeypatch, quiet=lambda h: monitor._in_quiet_hours(h),
                   announcements=lambda h: monitor._announcements_on(h), pushed=[])
    monitor._STATE.clear()
    yield
    monitor._STATE.clear()


@pytest.fixture
def pm(load):
    return load("package_monitor")


@pytest.fixture
def said(pm, load, monkeypatch):
    """Messages that reached the speakers, with quiet hours off and
    announcements on (a test may flip either afterwards)."""
    heard = _Heard()

    async def announce(hass, message, voice, targets, *args, **kwargs):
        heard.append(message)
        heard.calls.append((voice, list(targets), kwargs.get("context")))
        return True

    monkeypatch.setattr(load("tts_helper"), "async_announce", announce)
    monkeypatch.setattr(pm, "_in_quiet_hours", lambda h: False)
    monkeypatch.setattr(pm, "_announcements_on", lambda h: True)
    return heard


class _Heard(list):
    """The spoken messages, plus (voice, targets, context) for each in calls."""

    def __init__(self):
        super().__init__()
        self.calls = []


def _frames(pm, monkeypatch, *replies):
    """Make detect_on_camera hand back ``replies`` in turn; returns the list
    of cameras it was asked about. Running out of replies fails the test."""
    queue = list(replies)
    asked = []

    async def detect(hass, client, entity_id):
        asked.append(entity_id)
        assert queue, "detect_on_camera was called more often than expected"
        return queue.pop(0)

    monkeypatch.setattr(pm, "detect_on_camera", detect)
    return asked


async def _sweep(pm, hass, camera=DOOR):
    report = await pm.periodic_check(hass, None, "Sir", "tts.x", SPEAKERS,
                                     configured_camera=camera)
    await hass.drain()   # _log() also queues a semantic learning task
    return report


async def _eufy(pm, hass, role, camera=BELL):
    await pm.note_from_eufy(hass, "Sir", "tts.x", SPEAKERS, camera, role)
    await hass.drain()


# ── reading free text (the doorbell description) ────────────────────────────

@pytest.mark.parametrize("text, package, mail", [
    ("A UPS driver left a package by the door", True, False),
    ("The mail carrier left two envelopes", False, True),
    # A negation about one thing leaves a genuine other sighting alone.
    ("No mail today, but a package sits on the step", True, False),
    ("An Post van at the door", True, True),
    ("The postie is at the door", False, True),
    ("No postie visible", False, False),
    ("USPS", True, False),
    # A carrier name inside another word is not a carrier.
    ("The wiglsworth sign is visible", False, False),
    ("", False, False),
])
def test_text_reading(pm, text, package, mail):
    assert pm.detection_from_text(text) == _seen(package, mail)


@pytest.mark.parametrize("text", [
    "A person at the door, no package visible",
    "Someone rang the bell but is not carrying a delivery",
    "Visitor without any boxes at the door",
    "No sign of packages or mail on the porch",
    "There are no parcels left at the doorway",
])
def test_text_that_rules_a_delivery_out_reports_nothing(pm, text):
    found = pm.detection_from_text(text)
    assert (found["package"], found["mail"], found["count"]) == (False, False, 0)


_UK_IE_CARRIERS = ["an post", "dpd", "gls", "evri", "hermes", "yodel", "parcelforce",
                   "royal mail", "courier"]


@pytest.mark.parametrize("carrier", _UK_IE_CARRIERS)
@pytest.mark.parametrize("shape", ["{}", "A {} van is at the door"])
@pytest.mark.parametrize("case", [str.lower, str.upper])
def test_uk_and_irish_carriers_count_as_a_package(pm, carrier, shape, case):
    text = case(shape.format(carrier))
    assert pm.detection_from_text(text)["package"] is True, text


@pytest.mark.parametrize("carrier", _UK_IE_CARRIERS + ["amazon", "ups", "fedex", "usps", "dhl"])
def test_a_negated_carrier_is_not_a_package(pm, carrier):
    assert pm.detection_from_text(f"No {carrier} van visible")["package"] is False


# ── the vision check ────────────────────────────────────────────────────────

@pytest.fixture
def camera_mod(load):
    return load("camera")


async def test_a_blank_frame_is_never_shown_to_the_model(pm, camera_mod, fake_hass,
                                                         monkeypatch):
    async def frame(hass, eid):
        return b"\xff\xd8" + bytes(5000)       # JPEG shaped, but black

    asked_model = []
    monkeypatch.setattr(camera_mod, "_get_best_image", frame)
    monkeypatch.setattr(camera_mod, "_looks_blank", lambda raw: True)
    monkeypatch.setattr(camera_mod, "_camera_client", lambda *a, **k: asked_model.append(a))
    assert await pm.detect_on_camera(fake_hass, None, DOOR) is None
    assert asked_model == []


async def test_no_frame_at_all_is_no_answer(pm, camera_mod, fake_hass, monkeypatch):
    async def nothing(hass, eid):
        return None

    asked_model = []
    monkeypatch.setattr(camera_mod, "_get_best_image", nothing)
    monkeypatch.setattr(camera_mod, "_camera_client", lambda *a, **k: asked_model.append(a))
    assert await pm.detect_on_camera(fake_hass, None, DOOR) is None
    assert asked_model == []


@pytest.fixture
def model(load, camera_mod, monkeypatch):
    """detect_on_camera with a usable frame and a scripted model. Put the
    reply text under "reply"; the keyword arguments of the chat call are
    stored in the same dict."""
    script = {}

    async def frame(hass, eid):
        return b"jpeg"

    @contextlib.asynccontextmanager
    async def client(*args, **kwargs):
        yield object()

    async def chat(hass, client_, messages, **kwargs):
        script.update(kwargs)
        return types.SimpleNamespace(text=script["reply"])

    monkeypatch.setattr(camera_mod, "_get_best_image", frame)
    monkeypatch.setattr(camera_mod, "_looks_blank", lambda raw: False)
    monkeypatch.setattr(camera_mod, "_downscale_jpeg", lambda raw: raw)
    monkeypatch.setattr(camera_mod, "_cfg_opt", lambda hass, key, default=None: default)
    monkeypatch.setattr(camera_mod, "_camera_client", client)
    monkeypatch.setattr(load("providers.activity"), "execute_chat", chat)
    return script


@pytest.mark.parametrize("reply, expected", [
    # Python booleans from a local model, then two replies cut off mid way.
    ('{"package": False, "mail": False}', _seen()),
    ('{"package": false, "mail": fal', _seen()),
    ('{"package": ', None),
    # Prose with no JSON at all is never a sighting.
    ("I think there might be a package", None),
])
async def test_a_garbled_reply_is_never_a_delivery(pm, model, fake_hass, reply, expected):
    model["reply"] = reply
    assert await pm.detect_on_camera(fake_hass, None, DOOR) == expected


@pytest.mark.parametrize("reply, expected", [
    ('{"package": True, "mail": False}', _seen(package=True)),
    ('```json\n{"package": true, "mail": true, "count": 3, "description": "three boxes"}\n```',
     _seen(True, True, 3, "three boxes")),
    ('{"package": true, "mail": false, "count": "several"}', _seen(package=True)),
])
async def test_readable_replies_are_understood(pm, model, fake_hass, reply, expected):
    model["reply"] = reply
    assert await pm.detect_on_camera(fake_hass, None, DOOR) == expected


async def test_the_check_asks_at_temperature_zero(pm, model, fake_hass):
    model["reply"] = '{"package": true, "mail": false, "count": 1, "description": "box"}'
    found = await pm.detect_on_camera(fake_hass, None, DOOR)
    assert found == _seen(True, False, 1, "box")
    assert model["temperature"] == 0
    assert (model["role"], model["data_category"]) == ("package", "vision")


# ── the second look ─────────────────────────────────────────────────────────

BOX = _seen(True, description="box")


@pytest.mark.parametrize("before, first, second, expected", [
    # A new package must show on both frames, or it is dropped this round.
    ({}, BOX, _seen(), _seen(description="box")),
    ({}, BOX, None, _seen(description="box")),
    # Both frames agree: the larger count is kept.
    ({}, BOX, _seen(True, count=2), _seen(True, count=2, description="box")),
    ({}, _seen(True, count=3), BOX, _seen(True, count=3)),
    # Already known, or going away: no second look needed.
    ({"package": True}, BOX, None, BOX),
    ({"package": True}, _seen(), None, _seen()),
    # New mail is held to the same rule.
    ({}, _seen(mail=True), None, _seen()),
    ({}, _seen(mail=True), _seen(mail=True), _seen(mail=True)),
], ids=["unconfirmed", "second-frame-failed", "confirmed", "keeps-larger-count",
        "established", "pickup", "mail-unconfirmed", "mail-confirmed"])
def test_confirm_transitions(pm, before, first, second, expected):
    prev = {"package": False, "mail": False, **before}
    assert pm._confirm_transitions(prev, first, second) == expected


def test_confirm_pickup_keeps_the_package_without_a_matching_second_frame(pm):
    prev = {"package": True, "mail": False, "count": 2, "desc": "two boxes"}
    gone = _seen(description="empty step")
    kept = dict(gone, package=True, count=2, description="two boxes")
    assert pm._confirm_pickup(prev, gone, None) == kept
    assert pm._confirm_pickup(prev, gone, BOX) == kept
    assert pm._confirm_pickup(prev, gone, _seen()) is gone


# ── the periodic sweep ──────────────────────────────────────────────────────

async def test_a_confirmed_arrival_is_announced_once(pm, said, fake_hass, monkeypatch):
    fake_hass.states.set(DOOR, "idle")
    asked = _frames(pm, monkeypatch, BOX, BOX)
    report = await _sweep(pm, fake_hass)
    assert report == {"checked": 1, "cameras": [DOOR]}
    assert asked == [DOOR, DOOR]
    assert len(said) == 1 and "package has been delivered" in said[0]
    assert said.calls == [("tts.x", SPEAKERS, "package")]
    assert pm._STATE[DOOR]["package"] is True

    # A later sweep that still sees it says nothing more.
    _frames(pm, monkeypatch, BOX)
    await _sweep(pm, fake_hass)
    assert len(said) == 1


async def test_two_parcels_are_announced_as_two(pm, said, fake_hass, monkeypatch):
    fake_hass.states.set(DOOR, "idle")
    _frames(pm, monkeypatch, _seen(True, count=2), BOX)
    await _sweep(pm, fake_hass)
    assert len(said) == 1 and "2 packages have been delivered" in said[0]
    assert pm._STATE[DOOR]["count"] == 2


async def test_a_one_frame_hallucination_stays_quiet(pm, said, fake_hass, monkeypatch):
    fake_hass.states.set(DOOR, "idle")
    _frames(pm, monkeypatch, _seen(True, description="ghost box"),
            _seen(description="empty porch"))
    await _sweep(pm, fake_hass)
    assert said == []
    assert pm._STATE[DOOR]["package"] is False     # no phantom state either


async def test_the_sweep_is_skipped_in_quiet_hours(pm, said, fake_hass, monkeypatch):
    fake_hass.states.set(DOOR, "idle")
    monkeypatch.setattr(pm, "_in_quiet_hours", lambda h: True)
    asked = _frames(pm, monkeypatch)
    assert await _sweep(pm, fake_hass) == {"skipped": "quiet_hours"}
    assert asked == [] and said == []


async def test_a_camera_with_no_answer_is_not_counted(pm, said, fake_hass, monkeypatch):
    fake_hass.states.set(DOOR, "idle")
    _frames(pm, monkeypatch, None)
    assert await _sweep(pm, fake_hass) == {"checked": 0, "cameras": [DOOR]}
    assert DOOR not in pm._STATE and said == []


# Phantom pickups: a person standing in front of the parcel used to read as
# "taken", and with nobody home that is a theft alert.

def _tracked(pm, hass, camera, count=1, where="not_home"):
    pm._STATE[camera] = {"package": True, "mail": False, "count": count}
    hass.states.set("person.alex", where)


@pytest.mark.parametrize("second", [BOX, None], ids=["still-there", "second-capture-failed"])
async def test_an_away_pickup_needs_a_second_look(pm, said, fake_hass, monkeypatch, second):
    fake_hass.states.set(DOOR, "idle")
    _tracked(pm, fake_hass, DOOR)
    asked = _frames(pm, monkeypatch, _seen(description="someone in front"), second)
    await _sweep(pm, fake_hass)
    assert asked == [DOOR, DOOR]
    assert said == []
    assert pm._STATE[DOOR]["package"] is True


async def test_a_confirmed_away_pickup_still_alerts(pm, said, fake_hass, monkeypatch):
    fake_hass.states.set(DOOR, "idle")
    _tracked(pm, fake_hass, DOOR)
    empty = _seen(description="empty step")
    _frames(pm, monkeypatch, empty, empty)
    await _sweep(pm, fake_hass)
    assert len(said) == 1 and "no one is home" in said[0]
    assert pm._STATE[DOOR]["package"] is False


async def test_a_pickup_with_someone_home_takes_no_second_look(pm, said, fake_hass,
                                                               monkeypatch):
    fake_hass.states.set(DOOR, "idle")
    _tracked(pm, fake_hass, DOOR, where="home")
    asked = _frames(pm, monkeypatch, _seen())
    await _sweep(pm, fake_hass)
    assert asked == [DOOR]
    assert said == []
    assert pm._STATE[DOOR]["package"] is False


# ── Eufy's own package sensors ──────────────────────────────────────────────

async def test_eufy_delivered_announces_without_any_vision(pm, said, fake_hass, monkeypatch):
    asked = _frames(pm, monkeypatch)
    await _eufy(pm, fake_hass, "package_delivered")
    assert asked == []
    assert len(said) == 1 and "package has been delivered" in said[0]
    assert pm._STATE[BELL]["package"] is True


async def test_eufy_delivered_skips_vision_with_announcements_off(pm, said, fake_hass,
                                                                  monkeypatch):
    # The sensors stand in for the photo whether or not anything is said.
    monkeypatch.setattr(pm, "_announcements_on", lambda h: False)
    asked = _frames(pm, monkeypatch)
    await _eufy(pm, fake_hass, "package_delivered")
    assert asked == [] and said == []
    assert pm._STATE[BELL]["package"] is True


@pytest.mark.parametrize("where, alert", [("home", False), ("not_home", True)])
async def test_eufy_taken_only_alerts_when_nobody_is_home(pm, said, fake_hass, where, alert):
    # A pickup with someone in is not a theft.
    _tracked(pm, fake_hass, BELL, where=where)
    await _eufy(pm, fake_hass, "package_taken")
    assert pm._STATE[BELL]["package"] is False
    if alert:
        assert len(said) == 1 and "no one is home" in said[0]
    else:
        assert said == []


async def test_eufy_stranded_nags(pm, said, fake_hass):
    await _eufy(pm, fake_hass, "package_stranded")
    assert len(said) == 1 and "hasn't been picked up" in said[0]


async def test_eufy_stranded_keeps_quiet_hours(pm, said, fake_hass, monkeypatch):
    monkeypatch.setattr(pm, "_in_quiet_hours", lambda h: True)
    await _eufy(pm, fake_hass, "package_stranded")
    assert said == []


async def test_an_unknown_eufy_role_changes_nothing(pm, said, fake_hass):
    _tracked(pm, fake_hass, BELL, where="not_home")
    before = dict(pm._STATE[BELL])
    await _eufy(pm, fake_hass, "package_wobbled")
    assert pm._STATE[BELL] == before
    assert said == []


def test_a_eufy_camera_with_native_sensors_is_left_out_of_the_sweep(pm, load, fake_hass,
                                                                     monkeypatch):
    eufy = load("eufy")
    fake_hass.states.set(BELL, "idle")
    fake_hass.states.set("camera.porch_no_native", "idle")
    monkeypatch.setattr(eufy, "is_eufy_camera", lambda h, e: e == BELL)
    monkeypatch.setattr(eufy, "discover_roles",
                        lambda h, e: {"package_delivered": "binary_sensor.x"} if e == BELL else {})
    watched = pm.watched_cameras(fake_hass)
    assert BELL not in watched
    assert "camera.porch_no_native" in watched


# ── doorbell presses ────────────────────────────────────────────────────────
# The press description is about the caller. It can add a parcel or post,
# but saying nothing about a parcel is not proof that it has gone.

async def _press(pm, hass, text):
    await pm.note_from_doorbell(hass, None, "Sir", "tts.x", SPEAKERS, BELL, text)
    await hass.drain()


async def test_a_press_never_reports_a_tracked_parcel_as_taken(pm, said, fake_hass):
    _tracked(pm, fake_hass, BELL)
    await _press(pm, fake_hass, "A man in a blue jacket at the door")
    assert said == []
    assert pm._STATE[BELL]["package"] is True


async def test_a_press_about_post_keeps_the_tracked_parcel(pm, said, fake_hass):
    _tracked(pm, fake_hass, BELL, count=2)
    await _press(pm, fake_hass, "The postman is holding letters")
    assert len(said) == 1 and "mail has arrived" in said[0]
    assert pm._STATE[BELL]["package"] is True
    assert pm._STATE[BELL]["count"] == 2
    assert pm._STATE[BELL]["mail"] is True


async def test_a_press_that_rules_a_delivery_out_records_nothing(pm, said, fake_hass):
    await _press(pm, fake_hass, "A visitor at the door, no package visible")
    assert BELL not in pm._STATE
    assert said == []


async def test_a_press_that_shows_a_parcel_announces_it(pm, said, fake_hass):
    await _press(pm, fake_hass, "A courier is leaving a parcel on the step")
    assert len(said) == 1 and "package has been delivered" in said[0]
    assert pm._STATE[BELL]["package"] is True


# ── a package taken while away ────────────────────────────────────────────────

@pytest.fixture
def pm_hh(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    mod = load("package_monitor")
    for d in (mod._STATE, mod._LAST_SPOKEN, mod._TRIGGER_LAST):
        d.clear()
    yield mod
    for d in (mod._STATE, mod._LAST_SPOKEN, mod._TRIGGER_LAST):
        d.clear()


@pytest.fixture
def pkg(pm_hh, load, monkeypatch):
    sw = {"quiet": False, "spoken": [], "pushed": []}
    monkeypatch.setattr(pm_hh, "_in_quiet_hours", lambda h: sw["quiet"])
    monkeypatch.setattr(pm_hh, "_announcements_on", lambda h: True)
    monkeypatch.setattr(pm_hh, "_log", lambda *a, **k: None)
    monkeypatch.setattr(pm_hh, "_now_mono", lambda: 1000.0)

    async def _announce(hass, msg, *a, **k):
        sw["spoken"].append(msg)
        return True
    monkeypatch.setattr(load("tts_helper"), "async_announce", _announce)
    # 8.44.0: package messages go out through delivery.py.
    from conftest import route_delivery
    route_delivery(load, monkeypatch, quiet=lambda h: sw["quiet"], pushed=sw["pushed"])
    return sw


async def _package_then_gone(pm_hh, hass):
    cam = "camera.front_door"
    for present in (True, False):
        await pm_hh.evaluate(hass, None, "Sir", "tts.x", ["media_player.y"], cam,
                          {"package": present, "mail": False,
                           "count": 1 if present else 0, "description": ""})


async def test_a_package_taken_while_away_reaches_the_phones(pm_hh, pkg, fake_hass):
    fake_hass.states.set("person.abi", "not_home")
    await _package_then_gone(pm_hh, fake_hass)
    assert len(pkg["pushed"]) == 1 and "while no one is home" in pkg["pushed"][0]
    # The spoken line is unchanged.
    assert pkg["spoken"][-1] == pkg["pushed"][0]


async def test_in_quiet_hours_it_is_pushed_but_not_spoken(pm_hh, pkg, fake_hass):
    pkg["quiet"] = True
    fake_hass.states.set("person.abi", "not_home")
    await _package_then_gone(pm_hh, fake_hass)
    assert len(pkg["pushed"]) == 1 and pkg["spoken"] == []
    assert "while no one is home" in pkg["pushed"][-1]


@pytest.mark.parametrize("state", ["home", "unknown"])
async def test_nothing_is_pushed_unless_the_residents_are_away(pm_hh, pkg, fake_hass, state):
    fake_hass.states.set("person.abi", state)
    await _package_then_gone(pm_hh, fake_hass)
    assert pkg["pushed"] == []
