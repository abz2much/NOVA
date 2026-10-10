"""Moving every alert source onto alert_path (8.22.0) changed no wording and
no delivery.

Each test drives a source's real entry point with the residents home, away
and unknown, and pins the exact words and where they went: the speakers, the
phones, both or neither. The file only uses names that existed before
8.22.0, so it also passes against the 8.21.0 code. All state here is fake.
Nothing touches a real speaker, phone, alarm or lock.
"""
import sys
import types

import pytest

SPEAKER = "media_player.hall"
PEOPLE = {"home": "home", "away": "not_home", "unknown": "unknown"}


def _people(hass, residents):
    hass.states.set("person.abi", PEOPLE[residents])


# ── core actions (_emit_action): intrusion, lockdown, offers ────────────────

@pytest.fixture
def core(load, fake_hass, monkeypatch):
    cc = load("cognitive_core")
    hab = load("habituation")
    monkeypatch.setattr(hab, "is_quiet", lambda k: False)
    monkeypatch.setattr(hab, "record", lambda *a, **k: None)
    out = {"spoken": [], "pushed": []}

    async def _push(hass, config, message, action_type, snap=None, **kw):
        out["pushed"].append((action_type, message))
    monkeypatch.setattr(cc, "_push_notification", _push)
    monkeypatch.setattr(cc, "_notify_all_devices", _push)
    tts = load("tts_helper")
    monkeypatch.setattr(tts, "resolve_tts_for_context", lambda *a, **k: "tts.x")

    async def _announce(hass, message, tts_entity, targets, **kw):
        out["spoken"].append((message, list(targets)))
    monkeypatch.setattr(tts, "async_announce", _announce)
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "_in_quiet_hours", lambda *a: False)
    fake_hass.states.set(SPEAKER, "idle")
    out["cc"] = cc
    return out


async def _emit(core, hass, urgency, sleeping=False, **extra):
    action = dict({"type": "test_alert", "urgency": urgency,
                   "message": "Test alert."}, **extra)
    await core["cc"]._emit_action(hass, {"broadcast_group": SPEAKER}, action, sleeping)
    return core["spoken"], [t for t, _ in core["pushed"]]


SAID = [("Test alert.", [SPEAKER])]


@pytest.mark.parametrize("urgency,residents,spoken,pushed", [
    ("critical", "home", SAID, ["test_alert"]),
    ("critical", "away", SAID, ["test_alert"]),
    ("critical", "unknown", SAID, ["test_alert"]),
    ("high", "home", SAID, ["test_alert"]),
    ("high", "away", [], ["test_alert"]),
    ("high", "unknown", [], ["test_alert"]),
    ("medium", "home", SAID, []),
    # Medium while nobody is known to be home goes to the phones (it reached
    # no one): everyone away since 8.23.0, presence unknown since 8.23.1.
    ("medium", "away", [], ["test_alert"]),
    ("medium", "unknown", [], ["test_alert"]),
])
async def test_core_action(core, fake_hass, urgency, residents, spoken, pushed):
    _people(fake_hass, residents)
    assert await _emit(core, fake_hass, urgency) == (spoken, pushed)


@pytest.mark.parametrize("residents", PEOPLE)
async def test_core_action_asleep_or_phone_only(core, fake_hass, residents):
    _people(fake_hass, residents)
    assert await _emit(core, fake_hass, "high", sleeping=True) == ([], ["test_alert"])
    assert await _emit(core, fake_hass, "high", phone_only=True) == ([], ["test_alert"] * 2)
    assert core["pushed"][0][1] == "Test alert."


async def test_core_intrusion_while_armed_away_is_still_spoken(core, fake_hass):
    """An intruder's own movement makes the speakers route as someone home,
    so the first intrusion alert while armed away is spoken and pushed."""
    _people(fake_hass, "away")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert await _emit(core, fake_hass, "high", type="intrusion_investigating") == (
        SAID, ["intrusion_investigating"])


# ── observer ────────────────────────────────────────────────────────────────

@pytest.fixture
def observer(load, fake_hass, monkeypatch):
    o = load("observer")
    rc = load("reasoning_cache")
    db = load("database")
    llm = load("llm_provider")
    out = {"spoken": [], "pushed": [], "recorded": [], "urgency": "medium"}

    async def _classify(*a, **kw):
        return {"worth_considering": True, "urgency": out["urgency"], "category": "appliance"}

    async def _decide(*a, **kw):
        return {"speak": True, "message": "The washer has a problem.", "urgency": out["urgency"]}

    async def _speak(message, *, targets):
        out["spoken"].append((message, list(targets)))
        return targets

    async def _notify(message, *, urgency):
        out["pushed"].append((message, urgency))

    async def _no_llm(*a, **k):
        raise AssertionError("the provider must not be asked")

    monkeypatch.setattr(llm, "chat_with_activity", _no_llm)
    monkeypatch.setattr(o.classifier, "classify", _classify)
    monkeypatch.setattr(o.reasoning_loop, "decide", _decide)
    monkeypatch.setattr(o, "_speak", _speak)
    monkeypatch.setattr(o, "_send_notification", _notify)
    monkeypatch.setattr(o, "_is_announcements_enabled", lambda: True)
    monkeypatch.setattr(o, "_get_announcement_speakers", lambda: [SPEAKER])
    monkeypatch.setattr(rc, "remember", lambda *a, **k: None)
    monkeypatch.setattr(db, "save_activity", lambda **k: None)
    monkeypatch.setattr(o.audio_routing, "entity_area", lambda h, e: None)
    monkeypatch.setattr(o.sleep_detection, "is_sleeping", lambda *a, **k: (False, ""))
    monkeypatch.setattr(o.sleep_detection, "_in_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(o.output_gate, "can_announce", lambda **kw: (True, ""))
    monkeypatch.setattr(o.output_gate, "record_announcement",
                        lambda **kw: out["recorded"].append(kw["was_spoken"]))
    monkeypatch.setattr(o.output_gate, "recent_announcements", lambda n=5: [])
    monkeypatch.setattr(o._STATE, "hass", fake_hass)
    monkeypatch.setattr(o._STATE, "config", {})
    fake_hass.states.set(SPEAKER, "idle")

    def run(urgency):
        out["urgency"] = urgency
        mk = lambda s: types.SimpleNamespace(  # noqa: E731
            state=s, attributes={"friendly_name": "Washer problem", "device_class": "problem"})
        event = types.SimpleNamespace(data={
            "entity_id": "binary_sensor.washer_problem",
            "old_state": mk("off"), "new_state": mk("on")})
        return o._process_event(event)
    out["run"] = run
    return out


MSG = "The washer has a problem."


@pytest.mark.parametrize("urgency,residents,spoken,pushed,recorded", [
    ("high", "home", [(MSG, [SPEAKER])], [(MSG, "high")], [True]),
    ("high", "away", [], [(MSG, "high")], [False]),
    ("high", "unknown", [], [(MSG, "high")], [False]),
    ("medium", "home", [(MSG, [SPEAKER])], [], [True]),
    ("medium", "away", [], [(MSG, "medium")], [False]),
    ("medium", "unknown", [], [(MSG, "medium")], [False]),
])
async def test_observer(observer, fake_hass, urgency, residents, spoken, pushed, recorded):
    _people(fake_hass, residents)
    await observer["run"](urgency)
    assert observer["spoken"] == spoken
    assert observer["pushed"] == pushed
    assert observer["recorded"] == recorded


# ── appliances ──────────────────────────────────────────────────────────────

@pytest.fixture
def appliance(load, fake_hass, monkeypatch):
    am = load("appliance_monitor")
    og = load("output_gate")
    out = {"spoken": [], "pushed": [], "recorded": []}
    monkeypatch.setattr(og, "can_announce", lambda **kw: (True, ""))
    monkeypatch.setattr(og, "habit_note", lambda **kw: kw["message"])
    monkeypatch.setattr(og, "record_announcement",
                        lambda **kw: out["recorded"].append(kw["was_spoken"]))
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "is_sleeping", lambda *a, **k: (False, None))
    tts = load("tts_helper")
    monkeypatch.setattr(tts, "resolve_tts_for_context", lambda *a, **k: "tts.x")

    async def _announce(hass, message, tts_entity, targets, **kw):
        out["spoken"].append((message, list(targets)))
    monkeypatch.setattr(tts, "async_announce", _announce)
    nt = load("notify_targets")

    async def _send(hass, config, data, **kw):
        out["pushed"].append(data["message"])
        return ["notify.phone"]
    monkeypatch.setattr(nt, "async_send_configured_notifications", _send)
    monkeypatch.setattr(am, "_live_runtime_config", lambda: {"announcement_speakers": [SPEAKER]})
    am._MON.hass = fake_hass
    am._MON.config = {}
    fake_hass.states.set(SPEAKER, "idle")
    out["sensor"] = am._SensorState(
        entity_id="sensor.dryer_status", friendly_name="Dryer",
        appliance=am.ApplianceType.DRYER, discovery_method="native_status",
        peak_power=500.0)
    out["am"] = am
    return out


@pytest.mark.parametrize("residents,spoken,pushed,recorded", [
    ("home", [("Sir, the Dryer cycle is complete.", [SPEAKER])], [], [True]),
    ("away", [], ["The Dryer cycle is complete."], [False]),
    ("unknown", [], ["The Dryer cycle is complete."], [False]),
])
async def test_appliance(appliance, fake_hass, residents, spoken, pushed, recorded):
    _people(fake_hass, residents)
    await appliance["am"]._announce_done(appliance["sensor"], "dryer")
    assert appliance["spoken"] == spoken
    assert appliance["pushed"] == pushed
    assert appliance["recorded"] == recorded


# ── sentinel: left open reminders ───────────────────────────────────────────

@pytest.fixture
def sentinel(load, fake_hass, monkeypatch):
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    ev.async_call_later = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    sm = load("sentinel")
    out = {"spoken": [], "pushed": [], "sleeping": False}
    monkeypatch.setattr(sm, "save_sentinel_event", lambda *a, **k: None)
    monkeypatch.setattr(sm, "save_message", lambda *a, **k: None)

    async def _announce(hass, text, tts, speakers, **kw):
        out["spoken"].append(text)
    monkeypatch.setattr(sm, "async_announce", _announce)
    nt = load("notify_targets")

    async def _send(hass, config, data, **kw):
        out["pushed"].append(data["message"])
        return ["notify.phone"]
    monkeypatch.setattr(nt, "async_send_configured_notifications", _send)
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "is_sleeping", lambda *a, **k: (out["sleeping"], ""))
    monkeypatch.setattr(sm.nova_config, "announce_notify_only", lambda hass: False)
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                         friendly_name="Front Door")
    out["s"] = sm.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                               rules=[], entry=None)
    return out


RULE = {"id": "door_left_open",
        "message": "{honorific}, {friendly_name} has been open for {minutes} minutes."}


@pytest.mark.parametrize("residents", PEOPLE)
async def test_sentinel(sentinel, fake_hass, residents):
    _people(fake_hass, residents)
    await sentinel["s"]._announce_rule("binary_sensor.front_door", RULE, 10)
    text = sentinel["spoken"][0]
    assert text.endswith("Front Door has been open for 10 minutes.")
    assert sentinel["spoken"] == [text] and sentinel["pushed"] == [text]


@pytest.mark.parametrize("residents", PEOPLE)
async def test_sentinel_asleep_goes_to_phones(sentinel, fake_hass, residents):
    _people(fake_hass, residents)
    sentinel["sleeping"] = True
    await sentinel["s"]._announce_rule("binary_sensor.front_door", RULE, 10)
    assert sentinel["spoken"] == [] and len(sentinel["pushed"]) == 1


# ── hazards ─────────────────────────────────────────────────────────────────

@pytest.fixture
def hazard(load, monkeypatch):
    hm = load("hazard_monitor")
    cc = load("cognitive_core")
    tts = load("tts_helper")
    ar = load("audio_routing")
    order = []

    async def _push(hass, config, message, action_type, *a, **kw):
        order.append(("push", action_type, message))
    monkeypatch.setattr(cc, "_notify_all_devices", _push)
    monkeypatch.setattr(tts, "find_best_tts_entity", lambda hass: "tts.x")
    monkeypatch.setattr(ar, "broadcast_target", lambda hass, **kw: [SPEAKER])

    async def _announce(hass, message, tts_entity, targets, **kw):
        order.append(("speak", message, list(targets)))
    monkeypatch.setattr(tts, "async_announce", _announce)
    return hm, order


@pytest.mark.parametrize("residents", PEOPLE)
async def test_hazard(hazard, fake_hass, residents):
    hm, order = hazard
    _people(fake_hass, residents)
    await hm._deliver(fake_hass, "Seismic alert.", "quake", speak_text="Seismic alert.")
    await hm._deliver(fake_hass, "Warning lifted.", "warning")
    assert order == [("push", "hazard_earthquake", "Seismic alert."),
                     ("speak", "Seismic alert.", [SPEAKER]),
                     ("push", "hazard_weather", "Warning lifted.")]


# ── packages ────────────────────────────────────────────────────────────────

@pytest.fixture
def package(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    pm = load("package_monitor")
    for d in (pm._STATE, pm._LAST_SPOKEN, pm._TRIGGER_LAST):
        d.clear()
    out = {"quiet": False, "spoken": [], "pushed": []}
    monkeypatch.setattr(pm, "_in_quiet_hours", lambda h: out["quiet"])
    monkeypatch.setattr(pm, "_announcements_on", lambda h: True)
    monkeypatch.setattr(pm, "_log", lambda *a, **k: None)
    monkeypatch.setattr(pm, "_now_mono", lambda: 1000.0)

    async def _announce(hass, msg, *a, **k):
        out["spoken"].append(msg)
    monkeypatch.setattr(load("tts_helper"), "async_announce", _announce)

    async def _push(hass, msg):
        out["pushed"].append(msg)
    monkeypatch.setattr(pm, "_push_phones", _push)
    out["pm"] = pm
    yield out
    for d in (pm._STATE, pm._LAST_SPOKEN, pm._TRIGGER_LAST):
        d.clear()


async def _pkg(out, hass, **det):
    d = {"package": False, "mail": False, "count": 0, "description": ""}
    d.update(det)
    await out["pm"].evaluate(hass, None, "Sir", "tts.x", [SPEAKER], "camera.front_door", d)


@pytest.mark.parametrize("residents,removed_spoken,removed_pushed", [
    ("home", False, False), ("away", True, True), ("unknown", False, False)])
async def test_package(package, fake_hass, residents, removed_spoken, removed_pushed):
    _people(fake_hass, residents)
    await _pkg(package, fake_hass, package=True, count=1, mail=True)
    await _pkg(package, fake_hass, package=False, mail=True)
    removed = "Sir, a package was just removed from the front door while no one is home."
    assert package["spoken"] == (
        ["Sir, a package has been delivered to the front door.",
         "Sir, mail has arrived at the front door."]
        + ([removed] if removed_spoken else []))
    assert package["pushed"] == ([removed] if removed_pushed else [])


@pytest.mark.parametrize("residents", PEOPLE)
async def test_package_quiet_hours(package, fake_hass, residents):
    _people(fake_hass, residents)
    package["quiet"] = True
    await _pkg(package, fake_hass, package=True, count=2)
    await _pkg(package, fake_hass, package=False)
    assert package["spoken"] == []
    assert package["pushed"] == (
        ["Sir, a package was just removed from the front door while no one is home."]
        if residents == "away" else [])


@pytest.mark.parametrize("residents", PEOPLE)
async def test_package_stranded(package, fake_hass, residents):
    _people(fake_hass, residents)
    await package["pm"].note_from_eufy(fake_hass, "Sir", "tts.x", [SPEAKER],
                                       "camera.front_door", "package_stranded")
    assert package["spoken"] == ["Sir, a package at the front door hasn't been picked up yet."]


# ── doorbell ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("residents", PEOPLE)
async def test_doorbell(load, fake_hass, monkeypatch, residents):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    cam = load("camera")
    _people(fake_hass, residents)
    spoken = []

    async def _analyse(hass, call, *a, **kw):
        return {"success": True, "notable": True, "speak": "A courier is at the door.",
                "analysis": "A person in a red jacket holds a parcel."}
    monkeypatch.setattr(cam, "async_analyze_camera", _analyse)

    async def _no_media(hass, eid):
        return None
    monkeypatch.setattr(cam, "_fetch_event_media_image", _no_media)

    async def _announce(hass, message, tts_entity, targets, **kw):
        spoken.append((message, list(targets)))
    monkeypatch.setattr(cam, "async_announce", _announce)
    res = await cam._analyze_doorbell_press(
        fake_hass, None, "Sir", "tts.x", [SPEAKER], "camera.front_doorbell", "Doorbell pressed")
    assert spoken == [("A courier is at the door.", [SPEAKER])] and res["spoke"] is True
