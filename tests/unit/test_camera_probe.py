"""The camera diagnostic (camera.probe_camera) and the frame tiers it mirrors.

probe_camera walks override, backend, snapshot and stream wake in the same
order as _get_best_image and writes down what each tier did, so the panel can
say why a tile is blank. Every outward call is faked by the ``rig`` below: the
backend lookup, Home Assistant's snapshot, the stream wake and the blank-frame
check. Nothing sleeps.
"""
import sys
from types import ModuleType, SimpleNamespace

import pytest

# A real frame runs to tens of KB; anything at or under SMALL_SUSPECT_SIZE
# (12 000 bytes) is treated as a placeholder thumbnail.
REAL_FRAME = b"\xff\xd8" + b"\x55" * 20_000          # 20 002 bytes, "19KB"
THUMBNAIL = b"\xff\xd8" + b"\x55" * 2_500            # 2 502 bytes, not blank
BLACK = b"\x00" * 3_000                               # what an idle stream gives
CRUMB = b"\xff\xd8" + b"\x55" * 98                    # 100 bytes, under MIN_IMAGE_SIZE


class _Backend:
    """A camera backend that answers with ``body`` or raises ``error``."""

    def __init__(self, name, body=None, error=None):
        self.name = name
        self._body = body
        self._error = error
        self.asked = []

    async def fetch_best_image(self, hass, entity_id, cache):
        self.asked.append(entity_id)
        if self._error is not None:
            raise self._error
        return self._body


class _Rig:
    """Scripted stand-ins for everything probe_camera and _get_best_image
    reach out to. ``frames`` is consumed one per snapshot (the last entry
    repeats); an exception in it is raised instead of returned."""

    def __init__(self, cam, monkeypatch):
        self.cam = cam
        self._mp = monkeypatch
        self.frames = [REAL_FRAME]
        self.snapshots = []           # entity ids a snapshot was taken of
        self.wakes = []               # entity ids whose stream was woken
        self.wake_answer = True
        self.backend = None
        self.platform = None
        monkeypatch.setattr(cam, "_EVENT_CACHE", {})
        monkeypatch.setattr(cam, "find_backend", lambda hass, eid: self.backend)
        monkeypatch.setattr(cam, "_looks_blank", lambda body: body[:1] == b"\x00")
        monkeypatch.setattr(cam, "camera_get_image", self._snapshot)
        monkeypatch.setattr(cam, "_prewarm_stream", self._wake)

    async def _snapshot(self, hass, entity_id, timeout=10):
        self.snapshots.append(entity_id)
        item = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(content=item)

    async def _wake(self, hass, entity_id, settle=2.5):
        self.wakes.append(entity_id)
        return self.wake_answer

    def on_platform(self, platform):
        entry = SimpleNamespace(platform=platform)
        registry = SimpleNamespace(async_get=lambda eid: entry)
        self._mp.setattr(self.cam, "er", SimpleNamespace(async_get=lambda hass: registry))

    def override(self, load, mapping):
        settings = load("nova_config")
        self._mp.setattr(settings, "get", lambda key, default=None:
                         mapping if key == "camera_overrides" else default)


@pytest.fixture
def cam(load, monkeypatch):
    # camera_backends imports aiohttp at module level; this file must not rely
    # on another test file having stubbed it first.
    if sys.modules.get("aiohttp") is None:
        monkeypatch.setitem(sys.modules, "aiohttp", ModuleType("aiohttp"))
    return load("camera")


@pytest.fixture
def rig(cam, monkeypatch):
    return _Rig(cam, monkeypatch)


async def _probe(cam, hass, entity_id, state="idle", **attrs):
    if state is not None:
        hass.states.set(entity_id, state, **attrs)
    return await cam.probe_camera(hass, entity_id)


# ── entities Nova cannot fetch from at all ──────────────────────────────────

async def test_absent_entity_is_reported_missing_without_any_tier(cam, rig, fake_hass):
    report = await _probe(cam, fake_hass, "camera.porch", state=None)
    assert report["available"] is False
    assert report["state"] == "MISSING"
    assert report["verdict"] == "entity missing from HA"
    assert "missing" in report["verdict"]
    assert report["tiers"] == []
    assert rig.snapshots == [] and rig.wakes == []


async def test_unavailable_entity_stops_before_the_backend(cam, rig, fake_hass):
    rig.backend = _Backend("frigate", body=REAL_FRAME)
    report = await _probe(cam, fake_hass, "camera.porch", state="unavailable")
    assert report["available"] is False
    assert report["state"] == "unavailable"
    assert "UNAVAILABLE" in report["verdict"]
    assert report["tiers"] == []
    assert rig.backend.asked == [] and rig.snapshots == []


async def test_report_carries_the_camera_attributes(cam, rig, fake_hass):
    report = await _probe(cam, fake_hass, "camera.porch", brand="Acme",
                          model_name="Cam 2", supported_features=2,
                          frontend_stream_type="web_rtc")
    assert report["entity_id"] == "camera.porch"
    assert report["attrs"] == {"brand": "Acme", "model": "Cam 2",
                               "supported_features": 2,
                               "frontend_stream_type": "web_rtc"}
    assert report["platform"] is None


# ── the tier that succeeds decides the verdict ───────────────────────────────

async def test_plain_snapshot_success_never_wakes_the_stream(cam, rig, fake_hass):
    report = await _probe(cam, fake_hass, "camera.porch")
    assert report["verdict"] == "frames available via standard snapshot"
    assert report["tiers"] == [["backend-match", "none (generic camera path)"],
                               ["snapshot", "OK 19KB"]]
    assert any(t[0] == "snapshot" and t[1].startswith("OK") for t in report["tiers"])
    assert isinstance(report["elapsed_ms"], int) and report["elapsed_ms"] >= 0
    assert rig.wakes == []


async def test_backend_frame_short_circuits_every_later_tier(cam, rig, fake_hass):
    rig.backend = _Backend("frigate", body=REAL_FRAME)
    report = await _probe(cam, fake_hass, "camera.drive")
    assert report["verdict"] == "frames available via frigate backend"
    assert report["tiers"] == [["backend:frigate", "OK 19KB"]]
    assert len(report["tiers"]) == 1
    assert rig.backend.asked == ["camera.drive"]
    assert rig.snapshots == [] and rig.wakes == []


@pytest.mark.parametrize("cached, expected", [
    (False, "no image — no recent event media cached"),
    (True, "no image — cached event unusable"),
], ids=["nothing-cached", "event-cached"])
async def test_empty_backend_says_whether_an_event_was_cached(cam, rig, fake_hass,
                                                              cached, expected):
    rig.backend = _Backend("nest", body=None)
    if cached:
        rig.cam._EVENT_CACHE["camera.drive"] = {"event_id": "ev-1", "source": "nest"}
    report = await _probe(cam, fake_hass, "camera.drive")
    tiers = dict(report["tiers"])
    assert tiers["backend:nest"] == expected
    # The snapshot still gets its turn and succeeds.
    assert report["verdict"] == "frames available via standard snapshot"


async def test_backend_error_is_recorded_and_snapshot_still_tried(cam, rig, fake_hass):
    rig.backend = _Backend("eufy", error=ValueError("token expired"))
    report = await _probe(cam, fake_hass, "camera.drive")
    assert report["tiers"][0] == ["backend:eufy", "error: token expired"]
    assert report["verdict"] == "frames available via standard snapshot"
    assert rig.snapshots == ["camera.drive"]


async def test_backend_lookup_error_is_recorded_as_its_own_tier(cam, rig, fake_hass, monkeypatch):
    def _lookup_fails(hass, eid):
        raise KeyError("registry not ready")
    monkeypatch.setattr(cam, "find_backend", _lookup_fails)
    report = await _probe(cam, fake_hass, "camera.drive")
    assert report["tiers"][0][0] == "backend-match"
    assert report["tiers"][0][1].startswith("error: ")
    assert "registry not ready" in report["tiers"][0][1]
    assert report["verdict"] == "frames available via standard snapshot"


# ── the slow path: waking the stream ─────────────────────────────────────────

async def test_black_frame_then_wake_recovers(cam, rig, fake_hass):
    rig.frames = [BLACK, REAL_FRAME]
    report = await _probe(cam, fake_hass, "camera.hall")
    assert rig.wakes == ["camera.hall"]
    tiers = dict(report["tiers"])
    assert "BLANK" in tiers["snapshot"]
    assert tiers["wake-retry"].startswith("OK")
    assert tiers["wake-retry"] == "OK 19KB"
    assert report["verdict"] == "frames available after stream wake (slow path)"


async def test_thumbnail_is_suspect_rather_than_a_success(cam, rig, fake_hass):
    """A "successful" snapshot of only a couple of KB is a placeholder; the
    probe must not declare victory on it (v6.46.3)."""
    rig.frames = [THUMBNAIL, REAL_FRAME]
    report = await _probe(cam, fake_hass, "camera.hall", state="streaming")
    tiers = dict(report["tiers"])
    assert tiers["snapshot"].startswith("SUSPECT: only 2KB")
    assert "SUSPECT" in tiers["snapshot"]
    assert tiers["wake-retry"].startswith("OK")
    assert report["verdict"] == "frames available after stream wake (slow path)"


async def test_thumbnail_twice_gives_the_placeholder_verdict(cam, rig, fake_hass):
    rig.frames = [THUMBNAIL]
    report = await _probe(cam, fake_hass, "camera.hall", state="streaming")
    tiers = dict(report["tiers"])
    assert tiers["wake-retry"] == "no better (2502B)"
    assert "placeholder-sized" in report["verdict"]
    assert report["verdict"].startswith("only a placeholder-sized frame available — ")
    assert report["verdict"].endswith("check the camera's source integration.")


async def test_tiny_frame_and_failed_wake_point_at_the_source(cam, rig, fake_hass):
    rig.frames = [CRUMB]
    report = await _probe(cam, fake_hass, "camera.hall")
    assert report["tiers"][1:] == [["snapshot", "too small (100B)"],
                                   ["wake-retry", "still unusable (100B)"]]
    assert report["verdict"] == ("NO FRAME from any tier — check the camera's "
                                 "source integration.")


async def test_wake_retry_error_after_a_placeholder_keeps_both_notes(cam, rig, fake_hass):
    rig.frames = [THUMBNAIL, OSError("socket closed")]
    report = await _probe(cam, fake_hass, "camera.hall")
    tiers = dict(report["tiers"])
    assert tiers["wake-retry"] == "error: OSError: socket closed"
    assert report["verdict"].startswith(
        "only a placeholder-sized frame available. NO FRAME from any tier")


async def test_nest_with_nothing_anywhere_gets_nest_specific_advice(cam, rig, fake_hass):
    rig.backend = _Backend("nest", body=None)
    rig.on_platform("nest")
    rig.wake_answer = False
    rig.frames = [RuntimeError("stream not ready")]
    report = await _probe(cam, fake_hass, "camera.front_door")
    assert report["platform"] == "nest"
    tiers = dict(report["tiers"])
    assert "no recent event media" in tiers["backend:nest"]
    assert "RuntimeError" in tiers["snapshot"]
    assert tiers["snapshot"] == "error: RuntimeError: stream not ready"
    assert tiers["wake-retry"] == "error: RuntimeError: stream not ready"
    # Actionable guidance: wait for an event, then look at Pub/Sub.
    assert "Pub/Sub" in report["verdict"]
    assert "event" in report["verdict"]
    assert report["verdict"].startswith("NO FRAME from any tier. Nest cameras")


# ── camera_overrides: a restream stands in for the camera (v6.47.0) ─────────

async def test_probe_follows_and_reports_the_override(cam, rig, fake_hass, load):
    fake_hass.states.set("camera.restream_garden", "streaming")
    rig.override(load, {"camera.garden": "camera.restream_garden"})
    report = await _probe(cam, fake_hass, "camera.garden", state="streaming")
    assert report["override"] == "camera.restream_garden"
    tiers = dict(report["tiers"])
    assert "camera.restream_garden" in tiers["override"]
    assert report["tiers"][0] == ["override", "frames sourced from "
                                  "camera.restream_garden (camera_overrides)"]
    assert report["verdict"] == "frames available via standard snapshot"
    assert rig.snapshots == ["camera.restream_garden"]


async def test_override_to_a_missing_entity_is_ignored(cam, rig, fake_hass, load):
    fake_hass.states.set("camera.garden", "streaming")
    rig.override(load, {"camera.garden": "camera.not_there"})
    assert cam.resolve_camera_source(fake_hass, "camera.garden") == "camera.garden"
    report = await cam.probe_camera(fake_hass, "camera.garden")
    assert "override" not in report
    assert rig.snapshots == ["camera.garden"]


async def test_best_image_reads_only_from_the_override(cam, rig, fake_hass, load):
    fake_hass.states.set("camera.garden", "streaming")
    fake_hass.states.set("camera.restream_garden", "streaming")
    rig.override(load, {"camera.garden": "camera.restream_garden"})
    assert await cam._get_best_image(fake_hass, "camera.garden") == REAL_FRAME
    assert rig.snapshots == ["camera.restream_garden"]


# ── _get_best_image treats thumbnails the same way ──────────────────────────

@pytest.mark.parametrize("frames, best", [
    ([THUMBNAIL, REAL_FRAME], REAL_FRAME),    # the wake brings a real frame
    ([THUMBNAIL], THUMBNAIL),                 # a thumbnail beats nothing
], ids=["real-frame-wins", "thumbnail-last-resort"])
async def test_best_image_wakes_on_a_thumbnail(cam, rig, fake_hass, frames, best):
    rig.frames = list(frames)
    assert await cam._get_best_image(fake_hass, "camera.landing") == best
    assert rig.wakes == ["camera.landing"]
    assert len(rig.snapshots) == 2
