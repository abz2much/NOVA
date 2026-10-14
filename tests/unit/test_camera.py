"""Behaviour of camera.py: picking a frame, analysing it and reacting to camera
events. Everything outside the module (Home Assistant's camera API, the vision
and reasoning models, delivery, memory) is faked, so these tests pin what the
module itself decides."""
import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def cam(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    mod = load("camera")
    mod._EVENT_CACHE.clear()
    yield mod
    mod._EVENT_CACHE.clear()


def _pkg(cam):
    return cam.__name__.rsplit(".", 1)[0]


def _img(content):
    return types.SimpleNamespace(content=content)


def _jpeg(w=64, h=48, colour=(200, 120, 40), noisy=True):
    from PIL import Image
    img = Image.new("RGB", (w, h), colour)
    if noisy:
        px = img.load()
        for x in range(w):
            for y in range(h):
                px[x, y] = ((x * 7) % 255, (y * 11) % 255, ((x + y) * 5) % 255)
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


GOOD = b"\xff\xd8" + b"\x7f" * 20_000
TINY = b"\xff\xd8" + b"\x7f" * 5_000
BIGGER_TINY = b"\xff\xd8" + b"\x7f" * 8_000


def _registry(platforms=None, names=None):
    platforms = platforms or {}
    names = names or {}

    def entry(eid):
        if eid not in platforms and eid not in names:
            return None
        name, original = names.get(eid, (None, None))
        return types.SimpleNamespace(platform=platforms.get(eid), name=name,
                                     original_name=original)
    return types.SimpleNamespace(async_get=lambda hass: types.SimpleNamespace(async_get=entry))


# ── constants ────────────────────────────────────────────────────────────────

def test_constants(cam, load):
    assert cam.VISION_MODEL == load("const").DEFAULT_VISION_MODEL
    assert cam.MIN_IMAGE_SIZE == 2_000
    assert cam.SMALL_SUSPECT_SIZE == 12_000
    assert cam.EVENT_FRESH_SECONDS == 120


# ── small pure helpers ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    (None, None), ("", None),
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 2}\n```', {"a": 2}),
    ('```\n{"a": 3}```', {"a": 3}),
    ('Sure! {"a": 4} done', {"a": 4}),
    ("[1, 2]", [1, 2]),
    ("no json here", None),
    ("{broken", None),
    ('pre {"a": 5} mid {"b": 6} post', None),
])
def test_parse_json_obj(cam, raw, want):
    assert cam._parse_json_obj(raw) == want


@pytest.mark.parametrize("cat,want", [
    ("delivery", ("package", "delivered")), ("PACKAGE", ("package", "delivered")),
    ("mail", ("package", "mail")), ("person", ("person", None)),
    ("known_resident", ("person", None)), ("vehicle", ("vehicle", None)),
    (" Animal ", ("animal", None)), ("other", ("activity", None)),
    ("empty", (None, None)), ("ghost", (None, None)), (None, (None, None)),
])
def test_map_vision_category(cam, cat, want):
    assert cam._map_vision_category(cat) == want


@pytest.mark.parametrize("prompt,analysis,want", [
    ("", "A person waves", "person"), ("", "someone walks", "person"),
    ("", "an individual", "person"), ("", "A red CAR parks", "vehicle"),
    ("", "a truck", "vehicle"), ("Doorbell rang", "a quiet porch", "doorbell"),
    ("", "a package on the step", "doorbell"), ("", "leaves blowing", "motion"),
    (None, None, "motion"), ("", "a carton", "vehicle"),
])
def test_guess_detection_type(cam, prompt, analysis, want):
    assert cam._guess_detection_type(prompt, analysis) == want


@pytest.mark.parametrize("label,want", [
    ("person", "person"), ("Car", "vehicle"), ("truck", "vehicle"),
    ("motorcycle", "vehicle"), ("dog", "animal"), ("cat", "animal"),
    ("package", "package"), ("bicycle", "activity"),
    ("camera_person", "person"), ("doorbell_package", "package"),
    ("camera_motion", "activity"), ("chime", None), ("", None), (None, None),
    ("bird", None),
])
def test_map_bus_label(cam, label, want):
    assert cam._map_bus_label(label) == want


@pytest.mark.parametrize("text,want", [
    ("", False), (None, False),
    ("A woman stands at the door", True),
    ("The porch is empty", False),
    ("Nobody there, but a package sits on the step", True),
    ("Quiet street, nothing moving", False),
    ("A courier leaves a parcel", True),
])
def test_scene_has_subject(cam, text, want):
    assert cam._scene_has_subject(text) is want


def test_frigate_camera_name(cam):
    assert cam._frigate_camera_name("camera.front_door") == "front_door"
    assert cam._frigate_camera_name("front") == "front"


def test_display_and_merge_names(cam):
    names = {"camera.a": " Porch ", "camera.b": "  ", "camera.c": 5}
    assert cam.display_name("camera.a", "A cam", names) == "Porch"
    assert cam.display_name("camera.b", "B cam", names) == "B cam"
    assert cam.display_name("camera.c", None, names) == "camera.c"
    assert cam.display_name("camera.d", None, None) == "camera.d"
    merged = cam.merge_camera_name(names, "camera.d", " Drive ")
    assert merged["camera.d"] == "Drive" and "camera.d" not in names
    assert "camera.a" not in cam.merge_camera_name(names, "camera.a", "")
    assert cam.merge_camera_name(None, "camera.x", None) == {}


def test_resolve_camera_source(cam, load, fake_hass, monkeypatch):
    cfg = load("nova_config")
    overrides = {"camera.nest": "camera.twin", "camera.ghost": "camera.nowhere",
                 "camera.self": "camera.self"}
    monkeypatch.setattr(cfg, "get", lambda k, d=None: overrides if k == "camera_overrides" else d)
    fake_hass.states.set("camera.twin", "idle")
    fake_hass.states.set("camera.self", "idle")
    assert cam.resolve_camera_source(fake_hass, "camera.nest") == "camera.twin"
    assert cam.resolve_camera_source(fake_hass, "camera.ghost") == "camera.ghost"
    assert cam.resolve_camera_source(fake_hass, "camera.self") == "camera.self"
    assert cam.resolve_camera_source(fake_hass, "camera.other") == "camera.other"
    monkeypatch.setattr(cfg, "get", lambda k, d=None: None)
    assert cam.resolve_camera_source(fake_hass, "camera.nest") == "camera.nest"


# ── which cameras are in use ─────────────────────────────────────────────────

@pytest.mark.parametrize("value,want", [
    (["camera.a", 3], {"camera.a", "3"}), (("camera.b",), {"camera.b"}),
    ('["camera.c"]', {"camera.c"}), ("  ", set()), ("not json", set()),
    (None, set()), ({"camera.a": 1}, set()), ('{"x": 1}', set()),
])
def test_disabled_cameras(cam, load, monkeypatch, value, want):
    monkeypatch.setattr(load("nova_config"), "get", lambda k, d=None: value)
    assert cam._disabled_cameras() == want


def test_active_cameras(cam, fake_hass, monkeypatch):
    for eid in ("camera.a", "camera.b", "sensor.c"):
        fake_hass.states.set(eid, "idle")
    monkeypatch.setattr(cam, "_disabled_cameras", lambda: {"camera.b"})
    assert [s.entity_id for s in cam.active_camera_states(fake_hass)] == ["camera.a"]
    assert cam.active_cameras(fake_hass) == ["camera.a"]
    assert cam.active_camera_states(None) == []


def test_camera_integration(cam, fake_hass, monkeypatch):
    monkeypatch.setattr(cam, "er", _registry({"camera.f": "frigate", "camera.n": "nest",
                                              "camera.e": "eufy_security", "camera.g": "generic"}))
    assert [cam._camera_integration(fake_hass, e) for e in
            ("camera.f", "camera.n", "camera.e", "camera.g", "camera.x")] == \
        ["frigate", "nest", "eufy", "other", "other"]


def test_camera_friendly_name(cam, fake_hass, monkeypatch):
    fake_hass.states.set("camera.a", "idle", friendly_name="Porch")
    fake_hass.states.set("camera.b", "idle")
    monkeypatch.setattr(cam, "er", _registry(names={"camera.b": ("Side", "Orig"),
                                                    "camera.c": (None, "Orig C"),
                                                    "camera.d": (None, None)}))
    assert cam._camera_friendly_name(fake_hass, "camera.a") == "Porch"
    assert cam._camera_friendly_name(fake_hass, "camera.b") == "Side"
    assert cam._camera_friendly_name(fake_hass, "camera.c") == "Orig C"
    assert cam._camera_friendly_name(fake_hass, "camera.d") == "camera.d"
    assert cam._camera_friendly_name(fake_hass, "camera.z") == "camera.z"


# ── frame quality ────────────────────────────────────────────────────────────

def test_frame_stats_and_blank(cam):
    pytest.importorskip("PIL")
    noisy = _jpeg()
    mean, std, w, h = cam.frame_stats(noisy)
    assert (w, h) == (64, 48) and std > 8
    black = _jpeg(colour=(0, 0, 0), noisy=False)
    assert cam._looks_blank(black) is True
    dark_flat = _jpeg(colour=(30, 30, 30), noisy=False)
    assert cam._looks_blank(dark_flat) is False
    assert cam.frame_stats(b"not an image") is None
    assert cam._looks_blank(b"not an image") is False


def test_downscale(cam):
    pytest.importorskip("PIL")
    from PIL import Image
    small = _jpeg(100, 50)
    assert cam._downscale_jpeg(small, 200) is small
    out = cam._downscale_jpeg(_jpeg(400, 100), 200)
    assert Image.open(io.BytesIO(out)).size == (200, 50)
    assert cam._downscale_jpeg(b"junk") == b"junk"


def test_contact_sheet(cam):
    pytest.importorskip("PIL")
    from PIL import Image
    assert cam._make_contact_sheet([_jpeg()]) is None
    assert cam._make_contact_sheet([_jpeg(), b"junk"]) is None
    sheet = cam._make_contact_sheet([_jpeg(600, 300), _jpeg(100, 100), _jpeg(50, 80)],
                                    max_cols=2, cell_max=200)
    img = Image.open(io.BytesIO(sheet))
    # two columns of the widest cell (200) and two rows of the tallest (100) plus labels
    assert img.size == (2 * 200 + 3 * 6, 2 * (100 + 18) + 3 * 6)


def test_without_pillow_frames_pass_through(cam, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert cam.frame_stats(b"anything") is None
    assert cam._looks_blank(b"anything") is False
    assert cam._downscale_jpeg(b"anything", 10) == b"anything"
    assert cam._make_contact_sheet([b"a", b"b"]) is None


# ── grabbing a frame ─────────────────────────────────────────────────────────

class _Snapshots:
    """camera_get_image stand in returning scripted results in order."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, hass, entity_id, timeout=10):
        self.calls.append((entity_id, timeout))
        nxt = self.results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def grab(cam, monkeypatch):
    woke = []

    async def warm(hass, eid, settle=2.5):
        woke.append(eid)
        return True
    monkeypatch.setattr(cam, "_prewarm_stream", warm)
    monkeypatch.setattr(cam, "_looks_blank", lambda b: b == b"BLANK" * 1000)
    monkeypatch.setattr(cam, "find_backend", lambda h, e: None)
    monkeypatch.setattr(cam, "resolve_camera_source", lambda h, e: e)

    def use(*results):
        snaps = _Snapshots(*results)
        monkeypatch.setattr(cam, "camera_get_image", snaps)
        return snaps
    use.woke = woke
    return use


async def test_best_image_backend_first(cam, grab, monkeypatch, fake_hass):
    seen = {}

    class Backend:
        name = "frigate"

        async def fetch_best_image(self, hass, eid, cache):
            seen["cache"] = cache
            return b"BACKEND"
    monkeypatch.setattr(cam, "find_backend", lambda h, e: Backend())
    snaps = grab()
    assert await cam._get_best_image(fake_hass, "camera.a") == b"BACKEND"
    assert seen["cache"] is cam._EVENT_CACHE and snaps.calls == []


async def test_best_image_backend_failure_falls_through(cam, grab, monkeypatch, fake_hass):
    class Backend:
        name = "nest"

        async def fetch_best_image(self, hass, eid, cache):
            raise RuntimeError
    monkeypatch.setattr(cam, "find_backend", lambda h, e: Backend())
    grab(_img(GOOD))
    assert await cam._get_best_image(fake_hass, "camera.a") == GOOD


async def test_best_image_uses_override(cam, grab, monkeypatch, fake_hass):
    monkeypatch.setattr(cam, "resolve_camera_source", lambda h, e: "camera.twin")
    snaps = grab(_img(GOOD))
    await cam._get_best_image(fake_hass, "camera.a")
    assert snaps.calls == [("camera.twin", 10)]


async def test_best_image_good_first_frame(cam, grab, fake_hass):
    snaps = grab(_img(GOOD))
    assert await cam._get_best_image(fake_hass, "camera.a") == GOOD
    assert grab.woke == [] and snaps.calls == [("camera.a", 10)]


async def test_best_image_tiny_then_bigger(cam, grab, fake_hass):
    snaps = grab(_img(TINY), _img(BIGGER_TINY))
    assert await cam._get_best_image(fake_hass, "camera.a") == BIGGER_TINY
    assert grab.woke == ["camera.a"] and snaps.calls[1] == ("camera.a", 15)


async def test_best_image_tiny_kept_when_retry_no_better(cam, grab, fake_hass):
    grab(_img(TINY), _img(b"x" * 100))
    assert await cam._get_best_image(fake_hass, "camera.a") == TINY
    grab(_img(BIGGER_TINY), _img(TINY))
    assert await cam._get_best_image(fake_hass, "camera.a") == BIGGER_TINY
    grab(_img(TINY), RuntimeError("down"))
    assert await cam._get_best_image(fake_hass, "camera.a") is None


async def test_best_image_blank_or_failed(cam, grab, fake_hass):
    grab(_img(b"BLANK" * 1000), _img(b"BLANK" * 1000))
    assert await cam._get_best_image(fake_hass, "camera.a") is None
    grab(RuntimeError("no"), _img(GOOD))
    assert await cam._get_best_image(fake_hass, "camera.a") == GOOD
    grab(None, _img(b"x" * 2001))
    assert await cam._get_best_image(fake_hass, "camera.a") == b"x" * 2001
    grab(_img(None), _img(b"x" * 2000))
    assert await cam._get_best_image(fake_hass, "camera.a") is None


async def test_frame_sequence(cam, grab, monkeypatch, fake_hass):
    sleeps = []

    async def nap(s):
        sleeps.append(s)
    monkeypatch.setattr(cam.asyncio, "sleep", nap)
    monkeypatch.setattr(cam, "_downscale_jpeg", lambda b, m=1024: b[:4] + b"|%d" % m)
    grab(_img(GOOD), _img(b"BLANK" * 1000), RuntimeError("x"))
    frames = await cam._capture_frame_sequence(fake_hass, "camera.a", count=3, interval=0.5,
                                               max_dim=640)
    assert frames == [GOOD[:4] + b"|640"]
    assert sleeps == [0.5, 0.5] and grab.woke == ["camera.a"]


async def test_frame_sequence_falls_back_to_best(cam, grab, monkeypatch, fake_hass):
    async def nap(s):
        return None
    monkeypatch.setattr(cam.asyncio, "sleep", nap)
    monkeypatch.setattr(cam, "_downscale_jpeg", lambda b, m=1024: b"small")

    async def best(hass, eid):
        return b"BEST"
    monkeypatch.setattr(cam, "_get_best_image", best)
    grab(_img(b""))
    assert await cam._capture_frame_sequence(fake_hass, "camera.a", count=0) == [b"small"]

    async def none(hass, eid):
        return None
    monkeypatch.setattr(cam, "_get_best_image", none)
    grab(_img(b""))
    assert await cam._capture_frame_sequence(fake_hass, "camera.a", count=1) == []


@pytest.fixture
def ha_camera(monkeypatch):
    mod = sys.modules["homeassistant.components.camera"]
    for name in ("async_request_stream", "async_get_stream_source"):
        monkeypatch.delattr(mod, name, raising=False)
    return mod


async def test_prewarm_stream(cam, ha_camera, monkeypatch, fake_hass):
    sleeps = []

    async def nap(s):
        sleeps.append(s)
    monkeypatch.setattr(cam.asyncio, "sleep", nap)
    assert await cam._prewarm_stream(fake_hass, "camera.a") is False

    async def source(hass, eid):
        return "rtsp://x" if eid == "camera.a" else None
    monkeypatch.setattr(ha_camera, "async_get_stream_source", source, raising=False)
    assert await cam._prewarm_stream(fake_hass, "camera.a", settle=1) is True
    assert await cam._prewarm_stream(fake_hass, "camera.b") is False
    asked = []

    async def request(hass, eid, fmt=None):
        asked.append(fmt)
    monkeypatch.setattr(ha_camera, "async_request_stream", request, raising=False)
    assert await cam._prewarm_stream(fake_hass, "camera.a") is True
    assert asked == ["hls"] and sleeps == [1, 2.5]

    calls = []

    async def old_style(*args, **kw):
        calls.append((args, kw))
        if kw:
            raise TypeError
    monkeypatch.setattr(ha_camera, "async_request_stream", old_style, raising=False)
    assert await cam._prewarm_stream(fake_hass, "camera.a") is True
    assert calls[-1] == ((fake_hass, "camera.a", "hls"), {})

    async def boom(*a, **k):
        raise RuntimeError
    monkeypatch.setattr(ha_camera, "async_request_stream", boom, raising=False)
    assert await cam._prewarm_stream(fake_hass, "camera.a") is False


# ── event media ──────────────────────────────────────────────────────────────

def _nest_cache(cam, eid="camera.n", age=0, **over):
    entry = {"event_id": "ev1", "device_id": "dev1", "source": "nest",
             "ts": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=age)}
    entry.update(over)
    cam._EVENT_CACHE[eid] = entry


def _nest_data(media=None, raise_in_sub=False, device=True):
    class Store:
        async def async_get_media(self, device_id, event_id):
            if raise_in_sub:
                raise RuntimeError
            return media
    sub = types.SimpleNamespace(
        device_manager=types.SimpleNamespace(devices={"dev1": object()} if device else {}),
        media_store=Store())
    return types.SimpleNamespace(subscribers=[sub])


async def test_nest_event_image(cam, fake_hass):
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None
    _nest_cache(cam, source="frigate")
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None
    _nest_cache(cam, age=120)
    fake_hass.data["nest"] = _nest_data(types.SimpleNamespace(contents=GOOD))
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None
    _nest_cache(cam, age=10, event_id="")
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None
    _nest_cache(cam, age=10)
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") == GOOD
    fake_hass.data["nest"] = _nest_data(types.SimpleNamespace(contents=b"x" * 2000))
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None
    fake_hass.data["nest"] = _nest_data(raise_in_sub=True)
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None
    fake_hass.data["nest"] = _nest_data(types.SimpleNamespace(contents=GOOD), device=False)
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None
    fake_hass.data["nest"] = None
    assert await cam._fetch_nest_event_image(fake_hass, "camera.n") is None


async def test_event_media_by_integration(cam, load, monkeypatch, fake_hass):
    kinds = {"camera.n": "nest", "camera.e": "eufy", "camera.f": "frigate"}
    monkeypatch.setattr(cam, "_camera_integration", lambda h, e: kinds.get(e, "other"))

    async def nest(hass, eid):
        return b"NEST:" + eid.encode()
    monkeypatch.setattr(cam, "_fetch_nest_event_image", nest)
    eufy = load("eufy")
    monkeypatch.setattr(eufy, "discover_roles",
                        lambda h, e: {"event_image": "image.e_event"} if e == "camera.e" else {})

    async def eufy_img(hass, image_entity):
        return b"EUFY:" + image_entity.encode()
    monkeypatch.setattr(eufy, "async_fetch_event_image", eufy_img)
    assert await cam._fetch_event_media_image(fake_hass, "camera.n") == b"NEST:camera.n"
    assert await cam._fetch_event_media_image(fake_hass, "camera.e") == b"EUFY:image.e_event"
    assert await cam._fetch_event_media_image(fake_hass, "camera.f") is None
    kinds["camera.e2"] = "eufy"
    assert await cam._fetch_event_media_image(fake_hass, "camera.e2") is None


# ── bus events ───────────────────────────────────────────────────────────────

def _devices(cam, monkeypatch, devices, entities):
    monkeypatch.setattr(cam, "dr", types.SimpleNamespace(
        async_get=lambda h: types.SimpleNamespace(devices=devices)))
    monkeypatch.setattr(cam, "er", types.SimpleNamespace(
        async_get=lambda h: types.SimpleNamespace(entities=entities)))


def _dev(id_, *identifiers):
    return types.SimpleNamespace(id=id_, identifiers=set(identifiers))


def _ent(eid, device_id, domain="camera"):
    return types.SimpleNamespace(entity_id=eid, device_id=device_id, domain=domain)


def test_nest_device_to_camera(cam, monkeypatch, fake_hass):
    _devices(cam, monkeypatch,
             {"a": _dev("ha1", ("other", "enterprises/x/devices/NEST1")),
              "b": _dev("ha2", ("nest", "enterprises/x/devices/NEST1"))},
             {"1": _ent("sensor.s", "ha2", "sensor"), "2": _ent("camera.door", "ha2")})
    assert cam._nest_device_to_camera(fake_hass, "NEST1") == "camera.door"
    assert cam._nest_device_to_camera(fake_hass, "NEST9") is None
    monkeypatch.setattr(cam, "dr", None)
    assert cam._nest_device_to_camera(fake_hass, "NEST1") is None


def _event(data):
    return types.SimpleNamespace(data=data)


def test_handle_nest_event(cam, monkeypatch, fake_hass):
    monkeypatch.setattr(cam, "_nest_device_to_camera",
                        lambda h, d: "camera.door" if d == "D1" else None)
    cam._handle_nest_event(fake_hass, _event({"device_id": "D1", "nest_event_id": "E1",
                                              "event_type": "doorbell_chime"}))
    entry = cam._EVENT_CACHE["camera.door"]
    assert (entry["event_id"], entry["device_id"], entry["source"]) == ("E1", "D1", "nest")
    assert entry["ts"].tzinfo is None
    cam._handle_nest_event(fake_hass, _event({"nest_device_id": "D1", "event_id": "E2"}))
    assert cam._EVENT_CACHE["camera.door"]["event_id"] == "E2"
    cam._handle_nest_event(fake_hass, _event({"device_id": "D2", "event_id": "E3"}))
    cam._handle_nest_event(fake_hass, _event({"device_id": "D1"}))
    cam._handle_nest_event(fake_hass, _event(None))
    assert fake_hass.bus.fired == [
        ("nova_camera_event", {"entity_id": "camera.door", "source": "nest",
                               "label": "doorbell_chime", "confidence": None}),
        ("nova_camera_event", {"entity_id": "camera.door", "source": "nest",
                               "label": "motion", "confidence": None}),
    ]


def test_handle_frigate_event(cam, fake_hass):
    fake_hass.states.set("camera.front", "idle")
    cam._handle_frigate_event(fake_hass, _event({"type": "new", "after": {
        "camera": "Front", "id": "F1", "label": "person", "score": 0.876}}))
    cam._handle_frigate_event(fake_hass, _event({"type": "new", "before": {
        "camera": "front", "id": "F2", "top_score": 0.5}}))
    cam._handle_frigate_event(fake_hass, _event({"type": "new", "after": {
        "camera": "front", "id": "F3", "label": "car"}}))
    for ignored in ({"type": "update", "after": {"camera": "front", "id": "X"}},
                    {"type": "new", "after": {"camera": "garden", "id": "X"}},
                    {"type": "new", "after": {"id": "X"}}, {}):
        cam._handle_frigate_event(fake_hass, _event(ignored))
    entry = cam._EVENT_CACHE["camera.front"]
    assert (entry["event_id"], entry["device_id"], entry["source"]) == ("F3", "front", "frigate")
    assert fake_hass.bus.fired == [
        ("nova_camera_event", {"entity_id": "camera.front", "source": "frigate",
                               "label": "person", "confidence": 88}),
        ("nova_camera_event", {"entity_id": "camera.front", "source": "frigate",
                               "label": "object", "confidence": 50}),
        ("nova_camera_event", {"entity_id": "camera.front", "source": "frigate",
                               "label": "car", "confidence": None}),
    ]


def test_handle_frigate_event_bad_score_still_caches(cam, fake_hass):
    fake_hass.states.set("camera.front", "idle")
    cam._handle_frigate_event(fake_hass, _event({"type": "new", "after": {
        "camera": "front", "id": "F1", "score": "high"}}))
    assert cam._EVENT_CACHE["camera.front"]["event_id"] == "F1"
    assert fake_hass.bus.fired == []


async def test_semantic_camera_event(cam, load, monkeypatch, fake_hass):
    recorded = []

    async def record(hass, **kw):
        recorded.append(kw)
    monkeypatch.setattr(load("camera_semantic"), "record_event", record)
    cam._handle_semantic_camera_event(fake_hass, _event({
        "entity_id": "camera.f", "source": "frigate", "label": "person", "confidence": 88}))
    cam._handle_semantic_camera_event(fake_hass, _event({
        "entity_id": "camera.n", "source": "nest",
        "label": "camera_motion_" + "x" * 60, "confidence": None}))
    for ignored in ({"entity_id": "camera.e", "source": "eufy", "label": "person"},
                    {"source": "nest", "label": "person"},
                    {"entity_id": "camera.n", "source": "nest", "label": "chime"}):
        cam._handle_semantic_camera_event(fake_hass, _event(ignored))
    await fake_hass.drain()
    assert recorded == [
        {"label": "person", "camera_entity": "camera.f", "source": "frigate",
         "confidence": 88, "detail": "person"},
        {"label": "activity", "camera_entity": "camera.n", "source": "nest",
         "confidence": None, "detail": ("camera_motion_" + "x" * 60)[:40]},
    ]


def test_register_event_listeners(cam, fake_hass, monkeypatch):
    listened = []

    def listen(event, handler):
        listened.append((event, handler))
        return "unsub:" + event
    monkeypatch.setattr(fake_hass.bus, "async_listen", listen, raising=False)
    seen = []
    monkeypatch.setattr(cam, "_handle_nest_event", lambda h, e: seen.append(("nest", e)))
    monkeypatch.setattr(cam, "_handle_frigate_event", lambda h, e: seen.append(("frigate", e)))
    monkeypatch.setattr(cam, "_handle_semantic_camera_event",
                        lambda h, e: seen.append(("semantic", e)))
    unsubs = cam.register_event_listeners(fake_hass)
    assert unsubs == ["unsub:nest_event", "unsub:frigate_event", "unsub:nova_camera_event"]
    for _, handler in listened:
        handler("E")
    assert seen == [("nest", "E"), ("frigate", "E"), ("semantic", "E")]


def test_register_event_listeners_survives_failures(cam, fake_hass, monkeypatch):
    def listen(event, handler):
        if event == "frigate_event":
            raise RuntimeError
        return event
    monkeypatch.setattr(fake_hass.bus, "async_listen", listen, raising=False)
    assert cam.register_event_listeners(fake_hass) == ["nest_event", "nova_camera_event"]


# ── scene reasoning ──────────────────────────────────────────────────────────

@pytest.fixture
def chat(cam, load, monkeypatch):
    calls = []
    replies = []
    activity = load("providers.activity")

    async def execute_chat(hass, client, messages, **kw):
        calls.append({"client": client, "messages": messages, **kw})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return types.SimpleNamespace(text=reply)
    monkeypatch.setattr(activity, "execute_chat", execute_chat)
    observer = load("observer")
    monkeypatch.setattr(observer, "get_recent_context", lambda s=600: "")
    ol = load("output_language")
    monkeypatch.setattr(ol, "field_directive", lambda hass, field: " [lang:%s]" % field)
    monkeypatch.setattr(ol, "with_language", lambda hass, system: system + " [lang]")
    return types.SimpleNamespace(calls=calls, replies=replies, observer=observer)


async def test_reason_about_scene(cam, chat, fake_hass, monkeypatch):
    chat.replies.append(json.dumps({"notable": False, "category": "known_resident",
                                    "summary": "S" * 400, "speak": "  "}))
    monkeypatch.setattr(chat.observer, "get_recent_context", lambda s=600: "door opened")
    got = await cam._reason_about_scene(fake_hass, "CLIENT", "rm", "Porch", "desc", "person")
    assert got == {"notable": False, "category": "known_resident", "summary": "S" * 300,
                   "speak": None}
    call = chat.calls[0]
    assert call["client"] == "CLIENT" and call["role"] == "camera_reasoning"
    assert call["data_category"] == "text" and call["max_tokens"] == 220
    assert call["temperature"] == 0.3 and call["model_override"] == "rm"
    system, user = call["messages"][0]["content"], call["messages"][1]["content"]
    assert system.endswith(" [lang:speak]") and "ONLY a JSON object" in system
    assert user.startswith("Camera: Porch\nTime: ")
    assert "\nDetection: person\nVision description: desc\n" in user
    assert user.endswith("\nRecent home activity:\ndoor opened")


async def test_reason_about_scene_defaults(cam, chat, fake_hass, monkeypatch):
    chat.replies.append('{"speak": "Hello"}')
    got = await cam._reason_about_scene(fake_hass, None, "", "Porch", "desc", "motion")
    assert got == {"notable": True, "category": "motion", "summary": "desc", "speak": "Hello"}
    assert chat.calls[0]["model_override"] is None
    assert "Recent home activity" not in chat.calls[0]["messages"][1]["content"]
    monkeypatch.setattr(chat.observer, "get_recent_context",
                        lambda s=600: "quiet — no notable recent activity")
    chat.replies.append('{"category": "", "summary": ""}')
    got = await cam._reason_about_scene(fake_hass, None, "m", "Porch", "desc", "motion")
    assert got["category"] == "motion" and got["summary"] == "desc"
    assert "Recent home activity" not in chat.calls[1]["messages"][1]["content"]


@pytest.mark.parametrize("reply", ["not json", "[1, 2]", RuntimeError("down")])
async def test_reason_about_scene_falls_back(cam, chat, fake_hass, reply):
    chat.replies.append(reply)
    got = await cam._reason_about_scene(fake_hass, None, "m", "Porch", "desc", "vehicle")
    assert got == {"notable": True, "category": "vehicle", "summary": "desc", "speak": "desc"}


# ── analysing a camera ───────────────────────────────────────────────────────

@pytest.fixture
def env(cam, chat, load, monkeypatch, fake_hass):
    e = types.SimpleNamespace(hass=fake_hass, chat=chat, announced=[], delivered=[],
                              saved=[], observed=[], semantic=[], snapshots=[], logs=[],
                              scenes=[])
    fake_hass.states.set("camera.porch", "idle", friendly_name="Porch")
    monkeypatch.setattr(cam, "er", _registry({"camera.porch": "generic"}))

    async def best(hass, eid):
        return e.frame
    e.frame = GOOD
    monkeypatch.setattr(cam, "_get_best_image", best)

    class _Client:
        def __init__(self, fallback, binding):
            self.value = (fallback, binding)

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(cam, "_camera_client",
                        lambda hass, p, m, fallback, binding: _Client(fallback, binding))
    e.cfg = {}
    monkeypatch.setattr(cam, "_cfg_opt", lambda hass, k, d=None: e.cfg.get(k, d))
    monkeypatch.setattr(cam, "build_system_prompt", lambda hass, h, task: "SYS<%s>" % task)
    monkeypatch.setattr(cam, "save_message", lambda *a: e.saved.append(a))

    async def announce(hass, msg, tts, speakers, context=None):
        e.announced.append((msg, tts, list(speakers), context))
    monkeypatch.setattr(cam, "async_announce", announce)
    monkeypatch.setattr(chat.observer, "record_camera_event",
                        lambda *a: e.observed.append(a))

    async def record(hass, **kw):
        e.semantic.append(kw)
    monkeypatch.setattr(load("camera_semantic"), "record_event", record)
    monkeypatch.setattr(load("scene_memory"), "enabled", lambda: False)
    monkeypatch.setattr(load("proactive_briefing"), "record_snapshot",
                        lambda **kw: e.snapshots.append(kw))
    dl = load("delivery")

    async def deliver(hass, req):
        e.delivered.append(req)
        return types.SimpleNamespace(spoken=e.spoken)
    e.spoken = True
    monkeypatch.setattr(dl, "async_deliver", deliver)
    ws = types.ModuleType(_pkg(cam) + ".websocket")
    ws.nova_log = lambda cat, msg: e.logs.append((cat, msg))
    monkeypatch.setitem(sys.modules, _pkg(cam) + ".websocket", ws)
    monkeypatch.setattr(cam, "last_seen_at", lambda hass, eid: None)

    def call(**data):
        data.setdefault("entity_id", "camera.porch")
        return types.SimpleNamespace(data=data)
    e.call = call

    def judge(notable=True, category="person", summary="A person.", speak="Someone is here."):
        return json.dumps({"notable": notable, "category": category, "summary": summary,
                           "speak": speak})
    e.judge = judge
    return e


async def test_analyse_notable_scene(cam, env):
    env.chat.replies[:] = ["  A man holds a package.  ", env.judge(category="delivery")]
    res = await cam.async_analyze_camera(env.hass, env.call(), "FALLBACK", "sir", "tts.x",
                                         ["media_player.k"])
    await env.hass.drain()
    assert res == {"success": True, "analysis": "A man holds a package.", "summary": "A person.",
                   "notable": True, "category": "delivery", "speak": "Someone is here.",
                   "spoke": True, "camera": "Porch", "source": "other",
                   "image_bytes": len(GOOD)}
    vision, reasoning = env.chat.calls
    assert vision["client"] == ("FALLBACK", "vision") and vision["role"] == "vision"
    assert vision["data_category"] == "vision" and vision["max_tokens"] == 300
    assert vision["temperature"] == 0.7
    from_const = cam.VISION_MODEL
    assert vision["model_override"] == from_const
    system = vision["messages"][0]["content"]
    assert system.startswith("SYS<You are analysing the camera feed 'Porch'. ")
    assert system.endswith("> [lang]") and "Under 80 words" in system
    parts = vision["messages"][1]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[-1] == {"type": "text", "text":
                         "Describe what you see. Note anything unusual or worth attention."}
    assert reasoning["client"] == ("FALLBACK", "camera_reasoning")
    assert reasoning["model_override"] == "openai/gpt-oss-120b"
    assert "Detection: doorbell" in reasoning["messages"][1]["content"]
    assert env.saved == [("assistant", "[Camera: Porch] A person.", "camera")]
    assert env.observed == [("Porch", "A person.", "delivery", True)]
    assert env.semantic == [{"label": "package", "camera_entity": "camera.porch",
                             "source": "vision", "package_state": "delivered",
                             "detail": "delivery"}]
    assert env.snapshots == [{"camera_name": "Porch", "camera_entity": "camera.porch",
                              "analysis": "A person.", "detection_type": "delivery",
                              "urgency": "medium"}]
    req = env.delivered[0]
    assert (req.source, req.message, req.entity_id, req.category) == \
        ("camera", "Someone is here.", "camera.porch", "camera")
    assert req.speakers == ("media_player.k",) and req.tts_entity == "tts.x"
    assert req.tts_context == "camera"
    assert env.announced == []
    assert env.logs == [("CAMERA", "Porch [delivery] ⚠NOTABLE: A person.")]


async def test_analyse_quiet_scene_manual_and_auto(cam, env):
    env.chat.replies[:] = ["Empty drive.", env.judge(notable=False, category="empty",
                                                     summary="Nothing.", speak="")]
    res = await cam.async_analyze_camera(env.hass, env.call(prompt="Look"), None, "", None, [])
    assert res["spoke"] is True and res["speak"] is None
    assert env.announced == [("Empty drive.", None, [], "camera")]
    assert env.semantic == [] and env.snapshots[0]["urgency"] == "low"
    assert env.logs == [("CAMERA", "Porch [empty]: Nothing.")]
    env.chat.replies[:] = ["Empty drive.", env.judge(notable=False, speak="")]
    res = await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [],
                                         gate_announce=True)
    assert res["spoke"] is False and len(env.announced) == 1
    env.chat.replies[:] = ["x", env.judge()]
    env.spoken = False
    res = await cam.async_analyze_camera(env.hass, env.call(announce=False), None, "", None, [])
    assert res["spoke"] is False and len(env.delivered) == 0
    env.hass.close_pending()


async def test_analyse_notable_without_speech_delivered_flag(cam, env):
    env.chat.replies[:] = ["x", env.judge()]
    env.spoken = False
    res = await cam.async_analyze_camera(env.hass, env.call(), None, "", None, None)
    assert res["spoke"] is False and env.delivered[0].speakers == ()
    assert env.delivered[0].tts_entity == ""
    env.hass.close_pending()


async def test_analyse_no_image(cam, env):
    env.frame = None
    res = await cam.async_analyze_camera(env.hass, env.call(), None, "sir", "tts.x", ["m.k"])
    assert res == {"success": False, "error": "no_image", "camera": "Porch"}
    assert env.announced == [("sir, I could not get a usable image from Porch. This camera "
                              "may use WebRTC (no snapshot support) or be offline.",
                              "tts.x", ["m.k"], "camera")]
    assert env.logs == [("CAMERA", "Porch: no usable image (WebRTC/offline?)")]
    await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
    assert env.announced[1][0].startswith("I could not get")
    await cam.async_analyze_camera(env.hass, env.call(announce=False), None, "", None, [])
    assert len(env.announced) == 2 and env.chat.calls == []


async def test_analyse_forced_images_and_clip(cam, env, monkeypatch):
    env.chat.replies[:] = ["Moving.", env.judge()]
    monkeypatch.setattr(cam, "_make_contact_sheet", lambda frames: b"SHEET")
    res = await cam.async_analyze_camera(env.hass, env.call(interval=2), None, "", None, [],
                                         force_images=[b"A" * 10, None, b"B" * 5])
    assert res["image_bytes"] == 15
    parts = env.chat.calls[0]["messages"][1]["content"]
    assert len(parts) == 2
    system = env.chat.calls[0]["messages"][0]["content"]
    assert "a single contact sheet whose tiles are labelled Frame 1, Frame 2" in system
    assert "captured about 2s apart" in system and "Under 90 words" in system
    env.chat.replies[:] = ["Moving.", env.judge()]
    monkeypatch.setattr(cam, "_make_contact_sheet", lambda frames: None)
    await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [],
                                   force_images=[b"A", b"B", b"C"])
    parts = env.chat.calls[2]["messages"][1]["content"]
    assert len(parts) == 4
    assert "3 sequential frames in chronological order" in env.chat.calls[2]["messages"][0]["content"]
    env.hass.close_pending()


async def test_analyse_captures_clip_when_asked(cam, env, monkeypatch):
    asked = {}

    async def seq(hass, eid, count=3, interval=1.2, max_dim=1024):
        asked.update(count=count, interval=interval)
        return []
    monkeypatch.setattr(cam, "_capture_frame_sequence", seq)
    res = await cam.async_analyze_camera(env.hass, env.call(frames="4", interval=0, announce=False),
                                         None, "", None, [])
    assert res["error"] == "no_image" and asked == {"count": 4, "interval": 1.2}


async def test_analyse_recognition_hint(cam, env, monkeypatch):
    seen = {"name": "Sam", "confidence": 91.4, "age_seconds": 5, "is_confident": True}
    monkeypatch.setattr(cam, "last_seen_at", lambda hass, eid: dict(seen))
    env.chat.replies[:] = ["x", env.judge()]
    await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
    system = env.chat.calls[0]["messages"][0]["content"]
    assert ("IMPORTANT: Face recognition has identified the person in frame as 'Sam' "
            "(confidence 91%). Refer to them by name in your description.") in system
    for change in ({"age_seconds": 60}, {"confidence": 59}, {"name": "Unknown"},
                   {"is_confident": False}):
        seen = {"name": "Sam", "confidence": 91.4, "age_seconds": 5, "is_confident": True,
                **change}
        env.chat.replies[:] = ["x", env.judge()]
        env.chat.calls.clear()
        await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
        assert "IMPORTANT" not in env.chat.calls[0]["messages"][0]["content"]
    env.hass.close_pending()


async def test_preserved_quirk_recognition_hint_needs_is_confident(cam, env, load,
                                                                   monkeypatch):
    """recognition.last_seen_at never sets is_confident, so a real cache entry
    never produces the hint."""
    rec = load("recognition")
    rec._RECOGNITION_CACHE.clear()
    rec.remember_recognition("porch", "Sam", 95.0)
    monkeypatch.setattr(cam, "last_seen_at", rec.last_seen_at)
    env.chat.replies[:] = ["x", env.judge()]
    await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
    rec._RECOGNITION_CACHE.clear()
    rec._FACE_LOG.clear()
    assert "IMPORTANT" not in env.chat.calls[0]["messages"][0]["content"]
    env.hass.close_pending()


async def test_analyse_vision_errors(cam, env, monkeypatch):
    env.cfg.update(vision_provider="groq", vision_model="text-only")
    env.chat.replies[:] = [RuntimeError("rate limited")]
    res = await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
    assert res == {"success": False, "error": "rate limited"}
    assert env.logs[-1] == ("CAMERA", "Porch: vision error (groq/text-only) — rate limited")
    monkeypatch.setattr(cam, "_vision_model_rejects_images", lambda exc: True)
    env.chat.replies[:] = [RuntimeError("400")]
    res = await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
    friendly = ("vision model 'text-only' does not accept images — set 'vision_model' to a "
                "vision-capable model (e.g. 'moondream' on Ollama, or a Llama/Qwen vision "
                "model on Groq)")
    assert res == {"success": False, "error": friendly, "camera": "Porch"}
    assert env.logs[-1] == ("CAMERA", "Porch: " + friendly)
    assert env.saved == []


async def test_analyse_configured_models_and_blank_fallbacks(cam, env):
    env.cfg.update(vision_provider="", vision_model="", camera_reasoning_provider="ollama",
                   camera_reasoning_model="")
    env.chat.replies[:] = ["x", env.judge()]
    await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
    assert env.chat.calls[0]["model_override"] == cam.VISION_MODEL
    assert env.chat.calls[1]["model_override"] == "openai/gpt-oss-120b"
    env.hass.close_pending()


async def test_analyse_scene_memory(cam, env, load, monkeypatch):
    sm = load("scene_memory")
    monkeypatch.setattr(sm, "enabled", lambda: True)

    async def remember(hass, eid, text, location):
        env.scenes.append((eid, text, location))
    monkeypatch.setattr(sm, "async_remember", remember)
    monkeypatch.setattr(load("camera_semantic"), "resolve_location", lambda h, e: "front")
    env.chat.replies[:] = ["A fox.", env.judge(), "A fox.", env.judge()]
    await cam.async_analyze_camera(env.hass, env.call(), None, "", None, [])
    await cam.async_analyze_camera(env.hass, env.call(record_scene=False), None, "", None, [])
    assert env.scenes == [("camera.porch", "A fox.", "front")]
    env.hass.close_pending()


# ── doorbell and visitor flows ───────────────────────────────────────────────

@pytest.fixture
def flows(cam, load, monkeypatch, fake_hass):
    f = types.SimpleNamespace(calls=[], results=[], delivered=[], logged=[], packages=[],
                              media=None)

    async def analyse(hass, call, client, honorific, tts, speakers, gate_announce=False,
                      force_images=None):
        f.calls.append({"data": dict(call.data), "gate": gate_announce,
                        "force": force_images, "honorific": honorific, "tts": tts,
                        "speakers": speakers})
        return dict(f.results.pop(0))
    monkeypatch.setattr(cam, "async_analyze_camera", analyse)

    async def media(hass, eid):
        return f.media
    monkeypatch.setattr(cam, "_fetch_event_media_image", media)
    monkeypatch.setattr(cam, "_downscale_jpeg", lambda b, m=1024: b"small:" + b)
    monkeypatch.setattr(cam, "_camera_friendly_name", lambda h, e: "Door")

    async def nap(s):
        f.calls.append({"slept": s})
    monkeypatch.setattr(cam.asyncio, "sleep", nap)
    dl = load("delivery")

    async def deliver(hass, req):
        f.delivered.append(req)
        return types.SimpleNamespace(spoken=True)
    monkeypatch.setattr(dl, "async_deliver", deliver)
    monkeypatch.setattr(load("doorbell_training"), "log_event",
                        lambda *a: f.logged.append(a))

    async def note(hass, client, honorific, tts, speakers, eid, analysis):
        f.packages.append((eid, analysis))
    monkeypatch.setattr(load("package_monitor"), "note_from_doorbell", note)
    return f


async def test_doorbell_live_clip_wins(cam, flows, fake_hass):
    flows.results = [{"success": True, "analysis": "A man at the door", "notable": True,
                      "speak": "A visitor."}]
    res = await cam._analyze_doorbell_press(fake_hass, "C", "sir", "tts.x", ["m.k"],
                                            "camera.door", "Doorbell pressed")
    data = flows.calls[0]["data"]
    assert data["prompt"] == (
        "Doorbell pressed. Someone is at the door. Identify who is there and what they are "
        "doing — appearance, clothing, whether they carry a package or wait, any vehicle "
        "behind them. Focus on what the resident would want to know.")
    assert (data["announce"], data["frames"], data["interval"]) == (False, 3, 1.2)
    assert flows.calls[0]["gate"] is True and len(flows.calls) == 1
    assert res["spoke"] is True
    req = flows.delivered[0]
    assert (req.source, req.category, req.message, req.entity_id) == \
        ("doorbell", "doorbell", "A visitor.", "camera.door")
    assert req.speakers == ("m.k",) and req.tts_context == "camera"
    assert flows.logged == [("Door", "camera.door", "live", res)]
    assert flows.packages == [("camera.door", "A man at the door")]


async def test_doorbell_falls_back_to_event_media(cam, flows, fake_hass):
    flows.media = b"EV"
    flows.results = [{"success": True, "analysis": "An empty porch"},
                     {"success": True, "analysis": "A courier with a parcel", "notable": False}]
    res = await cam._analyze_doorbell_press(fake_hass, "C", "sir", None, [], "camera.door", "Ring")
    second = flows.calls[1]
    assert second["force"] == [b"small:EV"] and second["gate"] is True
    assert second["data"]["prompt"].endswith(
        " (This frame was captured at the moment the doorbell rang.)")
    assert second["data"]["announce"] is False and "frames" not in second["data"]
    assert res["analysis"] == "A courier with a parcel" and "spoke" not in res
    assert flows.logged[0][2] == "event-media" and flows.delivered == []


async def test_doorbell_keeps_live_when_media_shows_nobody(cam, flows, fake_hass):
    flows.media = b"EV"
    flows.results = [{"success": True, "analysis": "An empty porch"},
                     {"success": True, "analysis": "Still empty"}]
    res = await cam._analyze_doorbell_press(fake_hass, "C", "", None, [], "camera.door", "Ring")
    assert res["analysis"] == "An empty porch" and flows.logged[0][2] == "live"


async def test_doorbell_media_used_when_live_failed(cam, flows, fake_hass):
    flows.media = b"EV"
    flows.results = [{"success": False}, {"success": True, "analysis": "Still empty"}]
    res = await cam._analyze_doorbell_press(fake_hass, "C", "", None, [], "camera.door", "Ring")
    assert res["analysis"] == "Still empty" and flows.logged[0][2] == "event-media"


async def test_doorbell_total_failure(cam, flows, fake_hass):
    flows.results = [{"success": False}]
    res = await cam._analyze_doorbell_press(fake_hass, "C", "", None, [], "camera.door", "Ring")
    assert res == {"success": False}
    assert flows.logged == [] and flows.packages == []


async def test_auto_analyse(cam, flows, fake_hass):
    flows.results = [{"success": True}]
    await cam.async_auto_analyze_on_event(fake_hass, "C", "sir", "tts.x", ["m.k"], "camera.yard")
    assert flows.calls[0] == {"slept": 3}
    data = flows.calls[1]["data"]
    assert data == {"entity_id": "camera.yard", "prompt": (
        "Motion detected. Describe what you see clearly. If there is a person, describe "
        "their appearance and what they're doing. If there is a vehicle or package, note "
        "it. Focus on what the resident would want to know."),
        "announce": True, "frames": 3, "interval": 1.2}
    assert flows.calls[1]["gate"] is True


async def test_auto_analyse_doorbell(cam, flows, fake_hass, monkeypatch):
    seen = []

    async def press(*args):
        seen.append(args)
    monkeypatch.setattr(cam, "_analyze_doorbell_press", press)
    await cam.async_auto_analyze_on_event(fake_hass, "C", "sir", "tts.x", ["m.k"], "camera.d",
                                          reason="Ring", doorbell=True)
    assert seen == [(fake_hass, "C", "sir", "tts.x", ["m.k"], "camera.d", "Ring")]


async def test_visitor_observation(cam, flows, fake_hass, monkeypatch):
    flows.media = b"EV"
    flows.results = [{"success": True}]
    await cam.async_visitor_observation(fake_hass, "C", "sir", "camera.door")
    assert flows.calls[0] == {"slept": 2}
    call = flows.calls[1]
    assert call["force"] == [b"small:EV"] and call["tts"] is None and call["speakers"] == []
    assert call["data"]["announce"] is False and call["gate"] is True
    assert call["data"]["prompt"].startswith("A person was detected at the door (no doorbell")
    assert flows.logged == [("Door", "camera.door", "visitor", {"success": True})]


async def test_visitor_observation_live_and_nothing(cam, flows, fake_hass, monkeypatch):
    frames = [b"LIVE", None]

    async def best(hass, eid):
        return frames.pop(0)
    monkeypatch.setattr(cam, "_get_best_image", best)
    flows.results = [{"success": False}]
    await cam.async_visitor_observation(fake_hass, "C", "sir", "camera.door")
    assert flows.calls[1]["force"] == [b"small:LIVE"] and flows.logged == []
    await cam.async_visitor_observation(fake_hass, "C", "sir", "camera.door")
    assert len([c for c in flows.calls if "data" in c]) == 1


def test_fake_call(cam):
    assert cam._FakeCall({"a": 1}).data == {"a": 1}
