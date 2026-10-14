"""Behaviour of recognition.py: who Nova has seen on which camera.

Identity arrives from three MQTT feeds (Double Take matches, Frigate events
with a sub_label, Frigate tracked object face updates) and from Frigate's
`sensor.*_last_recognized_face` entities. These tests pin what each reader
returns and what each feed records and fires, using fake states and a fake
MQTT module."""
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def rec(load):
    mod = load("recognition")
    for box in (mod._FACE_LOG, mod._PERSON_LOG, mod._RECOGNITION_CACHE, mod._RECENT_EVENTS):
        box.clear()
    yield mod
    for box in (mod._FACE_LOG, mod._PERSON_LOG, mod._RECOGNITION_CACHE, mod._RECENT_EVENTS):
        box.clear()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _St:
    def __init__(self, entity_id, state, attrs=None, updated=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attrs if attrs is not None else {}
        self.last_updated = updated if updated is not None else datetime.now(timezone.utc)
        self.last_changed = self.last_updated


class _Hass:
    def __init__(self, sensors=(), mqtt=True):
        self._sensors = list(sensors)
        self._mqtt = mqtt
        self.fired = []
        hass = self

        class _States:
            def async_all(self, domain=None):
                return [s for s in hass._sensors
                        if domain is None or s.entity_id.startswith(domain + ".")]

        class _Services:
            def has_service(self, domain, service):
                return hass._mqtt and (domain, service) == ("mqtt", "publish")

        class _Bus:
            def async_fire(self, event, data=None):
                hass.fired.append((event, dict(data or {})))

        self.states = _States()
        self.services = _Services()
        self.bus = _Bus()


def _face(cam, name, score=None, **extra):
    attrs = dict(extra)
    if score is not None:
        attrs["score"] = score
    return _St(f"sensor.{cam}_last_recognized_face", name, attrs)


# ── constants ────────────────────────────────────────────────────────────────

def test_constants(rec):
    assert rec.MATCHES_TOPIC == "double-take/matches"
    assert rec.CAMERAS_TOPIC == "double-take/cameras"
    assert rec.TRACKED_OBJECT_TOPIC == "frigate/tracked_object_update"
    assert rec.CACHE_MAX_AGE == timedelta(hours=2)
    assert rec.CONFIDENCE_THRESHOLD == 60
    assert rec.RECENT_FACES_MAX_AGE_S == 86400
    assert rec._FACE_LOG.maxlen == 300
    assert rec._PERSON_LOG.maxlen == 100


# ── names and scores ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["unknown", " Unknown Person ", "unknown_face",
                                  "UNRECOGNIZED", "unrecognised", "", None, "none", "Null"])
def test_unknown_names(rec, name):
    assert rec.is_unknown_name(name) is True


@pytest.mark.parametrize("name", ["Sam", "unknownish", "nobody"])
def test_known_names(rec, name):
    assert rec.is_unknown_name(name) is False


@pytest.mark.parametrize("raw,want", [
    (0.93, 93.0), (1.0, 100.0), (88.0, 88.0), (None, 0.0), ("0.5", 50.0),
    ("junk", 0.0), ([1], 0.0), (0.12345, 12.3), (55.55, 55.5),
])
def test_normalize_score(rec, raw, want):
    assert rec._normalize_score(raw) == want


@pytest.mark.parametrize("sub,want", [
    ("Sam", ("Sam", 0.0)),
    ("  Sam  ", ("Sam", 0.0)),
    (["Sam", 0.92], ("Sam", 92.0)),
    (("Sam", 0.923), ("Sam", 92.3)),
    (["Sam", 95.0], ("Sam", 95.0)),
    (["Sam"], ("Sam", 0.0)),
    (["Sam", None], ("Sam", 0.0)),
    ([None, 0.5], ("", 50.0)),
    (["Sam", "junk"], ("", 0.0)),
    (None, ("", 0.0)),
    ("", ("", 0.0)),
    ([], ("", 0.0)),
    ({"name": "Sam"}, ("", 0.0)),
    (42, ("", 0.0)),
])
def test_parse_sub_label(rec, sub, want):
    assert rec._parse_sub_label(sub) == want


def test_camera_entity_from_name(rec):
    assert rec._camera_entity_from_name("Front_Door") == "camera.front_door"


# ── remembering ──────────────────────────────────────────────────────────────

def test_remember_recognition_fills_cache_and_log(rec):
    rec.remember_recognition("Front", "Sam", 91.0, source="frigate")
    entry = rec._RECOGNITION_CACHE["camera.front"]
    assert entry["name"] == "Sam" and entry["confidence"] == 91.0
    assert entry["unknown_count"] == 0
    assert abs((_now() - entry["ts"]).total_seconds()) < 5
    assert entry["ts"].tzinfo is None
    log = list(rec._FACE_LOG)
    assert len(log) == 1
    assert log[0]["camera_entity"] == "camera.front"
    assert log[0]["name"] == "Sam" and log[0]["confidence"] == 91.0
    assert log[0]["source"] == "frigate"


def test_unknown_count_climbs_only_for_unknown(rec):
    rec.remember_recognition("front", "unknown", 10.0)
    rec.remember_recognition("front", "Unknown", 10.0)
    assert rec._RECOGNITION_CACHE["camera.front"]["unknown_count"] == 2
    rec.remember_recognition("front", "unknown person", 10.0)
    assert rec._RECOGNITION_CACHE["camera.front"]["unknown_count"] == 0
    rec.remember_recognition("front", "unknown", 10.0)
    assert rec._RECOGNITION_CACHE["camera.front"]["unknown_count"] == 1


def test_remember_logs_odd_values_safely(rec):
    rec.remember_recognition("front", "Sam", None)
    assert rec._FACE_LOG[-1]["confidence"] == 0.0
    assert rec._RECOGNITION_CACHE["camera.front"]["confidence"] is None
    assert rec._FACE_LOG[-1]["source"] == ""


def test_preserved_quirk_remember_with_no_name_raises_after_logging(rec):
    with pytest.raises(AttributeError):
        rec.remember_recognition("front", None, 50.0)
    assert rec._FACE_LOG[-1]["name"] == ""
    assert "camera.front" not in rec._RECOGNITION_CACHE


def test_note_person_detected(rec):
    rec.note_person_detected("Garden")
    assert rec._PERSON_LOG[-1]["camera_entity"] == "camera.garden"
    assert rec._PERSON_LOG[-1]["ts"].tzinfo is None


def test_recent_windows(rec):
    rec.remember_recognition("front", "Sam", 90.0)
    rec.remember_recognition("garden", "Ann", 90.0)
    rec._FACE_LOG[0]["ts"] -= timedelta(seconds=120)
    got = rec.recent_face_events(60)
    assert [e["name"] for e in got] == ["Ann"]
    assert got[0]["age_seconds"] == 0
    assert [e["name"] for e in rec.recent_face_events(600)] == ["Sam", "Ann"]
    assert rec.recent_face_events(600)[0]["age_seconds"] == 120
    rec.note_person_detected("front")
    rec._PERSON_LOG[0]["ts"] += timedelta(seconds=30)            # in the future
    assert rec.recent_person_detections(600) == []
    rec.note_person_detected("hall")
    assert [e["camera_entity"] for e in rec.recent_person_detections(60)] == ["camera.hall"]


def test_recent_windows_never_raise(rec):
    rec._FACE_LOG.append({"ts": "bad"})
    rec._PERSON_LOG.append({"nope": 1})
    assert rec.recent_face_events(60) == []
    assert rec.recent_person_detections(60) == []


# ── cache readers ────────────────────────────────────────────────────────────

def test_last_seen_at(rec):
    assert rec.last_seen_at(None, "camera.front") is None
    rec.remember_recognition("front", "Sam", 91.0)
    got = rec.last_seen_at(None, "camera.front")
    assert got["name"] == "Sam" and got["camera_entity"] == "camera.front"
    assert got["age_seconds"] == 0 and got["unknown_count"] == 0
    rec._RECOGNITION_CACHE["camera.front"]["ts"] -= timedelta(hours=2, seconds=1)
    assert rec.last_seen_at(None, "camera.front") is None


def test_who_is_where_needs_confidence_and_freshness(rec):
    rec.remember_recognition("front", "Sam", 60.0)
    rec.remember_recognition("garden", "Ann", 59.9)
    rec.remember_recognition("hall", "Bo", 99.0)
    rec._RECOGNITION_CACHE["camera.hall"]["ts"] -= timedelta(hours=3)
    assert rec.who_is_where(None) == {"camera.front": "Sam"}


def test_context_string_ages_and_sources(rec):
    rec.remember_recognition("front_door", "Sam", 90.0)
    rec.remember_recognition("garden", "Ann", 90.0)
    rec.remember_recognition("hall", "Bo", 90.0)
    rec._RECOGNITION_CACHE["camera.garden"]["ts"] -= timedelta(minutes=5)
    rec._RECOGNITION_CACHE["camera.hall"]["ts"] -= timedelta(minutes=90)
    hass = _Hass([_face("porch", "sam", 0.8), _face("drive", "Cy", 0.876), _face("shed", "Di")])
    s = rec.recognition_context_string(hass)
    assert s == ("Recent faces: Sam seen at front door (just now); "
                 "Ann seen at garden (5m ago); Bo seen at hall (1h ago); "
                 "Cy recognized at drive ~88%; Di recognized at shed.")


def test_context_string_empty(rec):
    assert rec.recognition_context_string(_Hass()) == ""
    rec.remember_recognition("front", "Sam", 10.0)               # not confident
    assert rec.recognition_context_string(_Hass()) == ""


def test_context_string_survives_broken_states(rec):
    rec.remember_recognition("front", "Sam", 90.0)

    class _Bad:
        @property
        def states(self):
            raise RuntimeError
    assert rec.recognition_context_string(_Bad()) == "Recent faces: Sam seen at front (just now)."


# ── Frigate face sensors ─────────────────────────────────────────────────────

def test_read_face_sensors(rec):
    hass = _Hass([
        _face("dining_room", "Sam", 0.91),
        _face("hall", "Ann", None, confidence=77),
        _face("porch", "Bo", None, sub_label_score=0.5),
        _St("sensor.yard_last_recognized_face", "Cy", {"score": None, "confidence": 80}),
        _face("back", "None"), _face("a", "unknown"), _face("b", "Unavailable"),
        _face("c", "unknown_face"), _face("d", ""), _face("e", None),
        _St("sensor.other", "Sam"),
        _St("binary_sensor.x_last_recognized_face", "Sam"),
    ])
    got = rec.read_frigate_face_sensors(hass)
    assert got == [
        {"camera": "dining_room", "camera_entity": "camera.dining_room", "name": "Sam",
         "entity": "sensor.dining_room_last_recognized_face", "confidence": 91.0},
        {"camera": "hall", "camera_entity": "camera.hall", "name": "Ann",
         "entity": "sensor.hall_last_recognized_face", "confidence": 77.0},
        {"camera": "porch", "camera_entity": "camera.porch", "name": "Bo",
         "entity": "sensor.porch_last_recognized_face", "confidence": 50.0},
        {"camera": "yard", "camera_entity": "camera.yard", "name": "Cy",
         "entity": "sensor.yard_last_recognized_face", "confidence": 0.0},
    ]


def test_read_face_sensors_never_raises(rec):
    assert rec.read_frigate_face_sensors(None) == []
    bad = _face("x", "Sam")
    bad.attributes = 5                                     # .get fails for this one
    assert rec.read_frigate_face_sensors(_Hass([bad, _face("y", "Ann")]))[0]["name"] == "Ann"


def test_who_do_you_see_merges_sensor_and_cache(rec):
    rec.remember_recognition("front", "Sam", 90.0)
    rec.remember_recognition("garden", "Ann", 10.0)            # low confidence still counts
    rec.remember_recognition("hall", "unknown", 90.0)
    rec.remember_recognition("shed", "Bo", 90.0)
    rec._RECOGNITION_CACHE["camera.shed"]["ts"] -= timedelta(hours=3)
    hass = _Hass([_face("porch", "Sam", 0.9), _face("porch2", "Sam", 0.8)])
    got = rec.who_do_you_see(hass)
    assert got["any"] is True
    assert got["seen"] == ["Sam", "Ann"]
    assert [d["source"] for d in got["detail"]] == ["frigate_sensor", "frigate_sensor",
                                                     "recent_cache"]
    assert got["detail"][2] == {"camera_entity": "camera.garden", "name": "Ann",
                                "confidence": 10.0, "source": "recent_cache"}


def test_who_do_you_see_nothing(rec):
    assert rec.who_do_you_see(_Hass()) == {"seen": [], "detail": [], "any": False}


# ── Faces tab ────────────────────────────────────────────────────────────────

@pytest.fixture
def roster(load, monkeypatch):
    fr = load("face_roster")
    monkeypatch.setattr(fr, "normalized_names", lambda: {"sam"})
    return fr


def test_recent_faces_merges_and_keeps_newest(rec, roster):
    rec.remember_recognition("front", "Sam", 91.0, source="doubletake")
    rec._FACE_LOG[-1]["ts"] -= timedelta(seconds=50)
    rec.remember_recognition("front", "sam", 95.04, source="frigate")
    rec.remember_recognition("garden", "unknown", 0.0)
    old = datetime.now(timezone.utc) - timedelta(seconds=30)
    hass = _Hass([_St("sensor.porch_last_recognized_face", "Ann", {"score": 0.7}, old)])
    rows = rec.recent_faces(hass)
    assert [(r["name"], r["camera"]) for r in rows][:2] in (
        [("sam", "front"), ("unknown", "garden")], [("unknown", "garden"), ("sam", "front")])
    by = {r["name"]: r for r in rows}
    assert by["sam"]["confidence"] == 95.0 and by["sam"]["source"] == "frigate"
    assert by["sam"]["known"] is True and by["sam"]["resident"] is True
    assert by["unknown"]["known"] is False and by["unknown"]["resident"] is False
    assert by["unknown"]["source"] == "mqtt"
    assert by["Ann"] == {"name": "Ann", "camera": "porch", "camera_entity": "camera.porch",
                         "confidence": 70.0, "age_seconds": 30, "known": True,
                         "resident": False, "source": "frigate_sensor"}
    assert rows[-1]["name"] == "Ann"
    assert len(rows) == 3


def test_recent_faces_uses_cache_only_entries(rec, roster):
    rec._RECOGNITION_CACHE["camera.side"] = {"name": "Bo", "confidence": 80.0,
                                             "ts": _now(), "unknown_count": 0}
    rows = rec.recent_faces(_Hass())
    assert rows[0]["source"] == "recent_cache" and rows[0]["camera"] == "side"


def test_recent_faces_drops_old_and_unstamped(rec, roster):
    rec.remember_recognition("front", "Sam", 90.0)
    rec._FACE_LOG[-1]["ts"] -= timedelta(hours=25)
    rec._RECOGNITION_CACHE.clear()
    s = _face("x", "Ann")
    s.last_updated = s.last_changed = None
    naive = _face("y", "Cy")
    naive.last_updated = _now() - timedelta(seconds=10)
    rows = rec.recent_faces(_Hass([s, naive]))
    assert [(r["name"], r["age_seconds"]) for r in rows] == [("Cy", 10)]


def test_recent_faces_limit(rec, roster):
    for i in range(30):
        rec.remember_recognition(f"cam{i}", f"Person {i}", 80.0)
    assert len(rec.recent_faces(_Hass(), limit=5)) == 5
    assert len(rec.recent_faces(_Hass(), limit="x")) == 20
    assert len(rec.recent_faces(_Hass(), limit=999)) == 30
    assert len(rec.recent_faces(_Hass(), limit=0)) == 1


def test_preserved_quirk_recent_faces_broken_states_hides_everything(rec, roster):
    rec.remember_recognition("front", "Sam", 90.0)
    assert rec.recent_faces(None) == []


def test_source_status(rec, load, monkeypatch):
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda k, d=None: "Frigate" if k == "recognition_source" else d)
    st = rec.source_status(_Hass([_face("a", "None"), _face("b", "Sam")], mqtt=False))
    assert st == {"recognition_source": "frigate", "mqtt": False, "frigate_sensors": 2,
                  "seen_recently": False, "configured": True}
    assert rec.source_status(_Hass(mqtt=False))["configured"] is False
    assert rec.source_status(_Hass(mqtt=True))["configured"] is True
    rec.remember_recognition("front", "Sam", 90.0)
    assert rec.source_status(_Hass(mqtt=False))["seen_recently"] is True


def test_source_status_defaults_and_never_raises(rec, load, monkeypatch):
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda k, d=None: None)
    st = rec.source_status(None)
    assert st == {"recognition_source": "off", "mqtt": False, "frigate_sensors": 0,
                  "seen_recently": False, "configured": False}


# ── MQTT listener ────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, payload):
        self.payload = payload


@pytest.fixture
def mqtt(monkeypatch):
    subs = []
    mod = types.ModuleType("homeassistant.components.mqtt")

    async def async_subscribe(hass, topic, handler):
        if topic in mod.fail:
            raise RuntimeError("no broker")
        subs.append((topic, handler))
        return f"unsub:{topic}"
    mod.async_subscribe = async_subscribe
    mod.fail = set()
    mod.subs = subs
    monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", mod)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "mqtt", mod, raising=False)
    return mod


@pytest.fixture
def source(load, monkeypatch):
    cfg = load("nova_config")
    box = {"value": "off"}
    monkeypatch.setattr(cfg, "get",
                        lambda k, d=None: box["value"] if k == "recognition_source" else d)
    return box


def _handler(mqtt, topic):
    return next(h for t, h in mqtt.subs if t == topic)


@pytest.mark.parametrize("choice,topics", [
    ("off", ["frigate/events"]),
    (None, ["frigate/events"]),
    ("doubletake", ["double-take/matches", "frigate/events"]),
    ("frigate", ["frigate/events", "frigate/tracked_object_update"]),
    ("BOTH", ["double-take/matches", "frigate/events", "frigate/tracked_object_update"]),
])
async def test_listener_subscribes_by_source(rec, mqtt, source, choice, topics):
    source["value"] = choice
    unsubs = await rec.register_recognition_listener(_Hass())
    assert [t for t, _ in mqtt.subs] == topics
    assert unsubs == [f"unsub:{t}" for t in topics]


async def test_listener_needs_mqtt_service(rec, mqtt, source):
    source["value"] = "both"
    assert await rec.register_recognition_listener(_Hass(mqtt=False)) == []
    assert mqtt.subs == []


async def test_listener_without_mqtt_component(rec, monkeypatch, source):
    monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", None)
    monkeypatch.delattr(sys.modules["homeassistant.components"], "mqtt", raising=False)
    assert await rec.register_recognition_listener(_Hass()) == []


async def test_listener_survives_failed_subscriptions(rec, mqtt, source):
    source["value"] = "both"
    mqtt.fail = {"double-take/matches", "frigate/events"}
    unsubs = await rec.register_recognition_listener(_Hass())
    assert unsubs == ["unsub:frigate/tracked_object_update"]


async def test_listener_config_failure_means_off(rec, mqtt, load, monkeypatch):
    cfg = load("nova_config")

    def boom(*a, **k):
        raise RuntimeError
    monkeypatch.setattr(cfg, "get", boom)
    await rec.register_recognition_listener(_Hass())
    assert [t for t, _ in mqtt.subs] == ["frigate/events"]


async def test_doubletake_match(rec, mqtt, source):
    source["value"] = "doubletake"
    hass = _Hass()
    await rec.register_recognition_listener(hass)
    h = _handler(mqtt, "double-take/matches")
    h(_Msg(json.dumps({"camera": "Front", "match": {"name": "Sam", "confidence": 98.7}})))
    h(_Msg({"camera_name": "garden", "match": {"confidence": "40"}}))
    h(_Msg(b'{"camera": "x", "match": {}}'))
    h(_Msg('{"match": {"name": "Sam"}}'))
    h(_Msg("not json"))
    assert hass.fired == [
        ("nova_face_recognized", {"camera": "Front", "camera_entity": "camera.front",
                                  "name": "Sam", "confidence": 98.7, "is_unknown": False,
                                  "is_confident": True}),
        ("nova_face_recognized", {"camera": "garden", "camera_entity": "camera.garden",
                                  "name": "unknown", "confidence": 40.0, "is_unknown": True,
                                  "is_confident": False}),
    ]
    assert rec._FACE_LOG[0]["source"] == "doubletake"
    assert rec._RECOGNITION_CACHE["camera.garden"]["unknown_count"] == 1


async def test_preserved_quirk_doubletake_bad_confidence_raises(rec, mqtt, source):
    source["value"] = "doubletake"
    await rec.register_recognition_listener(_Hass())
    h = _handler(mqtt, "double-take/matches")
    with pytest.raises((TypeError, ValueError)):
        h(_Msg({"camera": "front", "match": {"name": "Sam", "confidence": None}}))


def _frigate(type_="new", label="person", score=0.9, camera="front", sub=None, eid="abcdef123"):
    after = {"label": label, "camera": camera, "top_score": score, "id": eid}
    if sub is not None:
        after["sub_label"] = sub
    return _Msg(json.dumps({"type": type_, "after": after}))


async def test_frigate_person_detection_always_runs(rec, mqtt, source):
    hass = _Hass()
    await rec.register_recognition_listener(hass)
    h = _handler(mqtt, "frigate/events")
    await h(_frigate(sub=["Sam", 0.95]))
    assert hass.fired == [("nova_person_detected", {
        "camera": "front", "camera_entity": "camera.front", "label": "person",
        "score": 0.9, "event_id": "abcdef123"})]
    assert rec._PERSON_LOG[-1]["camera_entity"] == "camera.front"
    ev = rec._RECENT_EVENTS["camera.front"]
    assert ev["event_id"] == "abcdef123" and ev["source"] == "frigate"
    assert ev["ts"].tzinfo is None
    assert rec._RECOGNITION_CACHE == {}                     # identity is off


@pytest.mark.parametrize("msg", [
    _frigate(type_="update"), _frigate(label="car"), _frigate(score=0.69),
    _frigate(score=None), _Msg("garbage"), _Msg(json.dumps({"type": "new"})),
])
async def test_frigate_events_ignored(rec, mqtt, source, msg):
    hass = _Hass()
    await rec.register_recognition_listener(hass)
    await _handler(mqtt, "frigate/events")(msg)
    assert hass.fired == [] and not rec._PERSON_LOG


async def test_frigate_sub_label_identity(rec, mqtt, source):
    source["value"] = "frigate"
    hass = _Hass()
    await rec.register_recognition_listener(hass)
    h = _handler(mqtt, "frigate/events")
    await h(_frigate(score=0.7, sub=["Sam", 0.95]))
    await h(_frigate(camera="garden", sub="Unknown Person"))
    await h(_frigate(camera="hall", sub=""))
    faces = [d for e, d in hass.fired if e == "nova_face_recognized"]
    assert faces == [
        {"camera": "front", "camera_entity": "camera.front", "name": "Sam",
         "confidence": 95.0, "is_unknown": False, "is_confident": True, "source": "frigate"},
        {"camera": "garden", "camera_entity": "camera.garden", "name": "Unknown Person",
         "confidence": 0.0, "is_unknown": True, "is_confident": False, "source": "frigate"},
    ]
    assert [e["source"] for e in rec._FACE_LOG] == ["frigate", "frigate"]
    assert len([e for e, _ in hass.fired if e == "nova_person_detected"]) == 3


async def test_tracked_object_face(rec, mqtt, source):
    source["value"] = "frigate"
    hass = _Hass()
    await rec.register_recognition_listener(hass)
    h = _handler(mqtt, "frigate/tracked_object_update")
    await h(_Msg(json.dumps({"type": "face", "name": " Sam ", "score": 0.93, "camera": "front"})))
    await h(_Msg(json.dumps({"type": "face", "name": "unknown", "score": 0.2})))
    for skip in ({"type": "lpr", "name": "Sam"}, {"type": "face", "name": "None"},
                 {"type": "face", "name": ""}, {"type": "face"}):
        await h(_Msg(json.dumps(skip)))
    await h(_Msg("garbage"))
    assert hass.fired == [
        ("nova_face_recognized", {"camera": "front", "camera_entity": "camera.front",
                                  "name": "Sam", "confidence": 93.0, "is_unknown": False,
                                  "is_confident": True, "source": "frigate_tracked_object"}),
        ("nova_face_recognized", {"camera": "", "camera_entity": "", "name": "unknown",
                                  "confidence": 20.0, "is_unknown": True,
                                  "is_confident": False, "source": "frigate_tracked_object"}),
    ]
    assert [e["source"] for e in rec._FACE_LOG] == ["frigate_tracked"]   # no camera, no record


def test_recognition_source_read_twice_with_off_default():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "custom_components/nova/recognition.py").read_text(encoding="utf-8")
    assert src.count('nova_config.get("recognition_source", "off")') == 2


# ── agent tool registration ──────────────────────────────────────────────────

def test_who_do_you_see_tool_registered(load):
    agent = load("agent")
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert "who_do_you_see" in names
    assert "who_do_you_see" in agent._TOOL_MAP
