"""Pin what the camera commands in ws_cameras.py do today (8.7.22, tests only).

nova/camera_snapshot, nova/rename_camera, nova/camera_location,
nova/camera_diagnostics, nova/compute_camera_coverage and nova/mmwave_overview.
nova/camera_location is the riskiest: it decides whether a camera counts as
indoor or outdoor, which the intrusion investigator and the outdoor event
filter both follow. The decorators are stubbed to pass through, so the
handler bodies run with a recording connection and fake camera, config and
log seams. Tests named test_current_behaviour_* pin behaviour that looks
wrong; each says why.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import pathlib
import sys
import types
from typing import Optional

import pytest

from fakes import FakeHass
from test_pin_ws_update_config import _Conn, _stub_ws_api


@pytest.fixture
def cams(load, monkeypatch):
    # ws_bridge looks _get_entry up on jc.websocket at call time.
    _stub_ws_api(monkeypatch)
    for name in ("jc.ws_cameras", "jc.websocket"):
        sys.modules.pop(name, None)
    load("websocket")
    mod = load("ws_cameras")
    logged = []
    monkeypatch.setattr(mod, "nova_log", lambda cat, msg: logged.append((cat, msg)))
    monkeypatch.setattr(mod, "_SNAP_LOG_TS", {})
    monkeypatch.setattr(mod, "_get_cameras", lambda hass: ["cams"])
    mod._logged = logged
    yield mod
    for name in ("jc.ws_cameras", "jc.websocket"):
        sys.modules.pop(name, None)


_CAMERA_PY = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "camera.py"


@pytest.fixture(autouse=True)
def camera_mod(monkeypatch):
    """A stand-in for camera.py, which imports aiohttp (not installed for the
    unit run). It carries the real merge_camera_name, compiled from
    camera.py's own source, and test-set frame and probe functions."""
    src = _CAMERA_PY.read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "merge_camera_name")
    mod = types.ModuleType("jc.camera")
    mod.Optional = Optional
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(_CAMERA_PY), "exec"),
         mod.__dict__)
    monkeypatch.setitem(sys.modules, "jc.camera", mod)
    monkeypatch.setattr(sys.modules["jc"], "camera", mod, raising=False)
    return mod


@pytest.fixture
def store(load, monkeypatch):
    nc = load("nova_config")
    data: dict = {}
    writes = []

    def _set(k, v):
        writes.append({k: v})
        data[k] = v
        return True

    def _set_many(updates):
        writes.append(dict(updates))
        data.update(updates)
        return True
    monkeypatch.setattr(nc, "set", _set)
    monkeypatch.setattr(nc, "set_many", _set_many)
    monkeypatch.setattr(nc, "get", lambda k, d=None: data.get(k, d))
    return types.SimpleNamespace(data=data, writes=writes)


def _hass(*cameras):
    hass = FakeHass()
    hass.config_entries = types.SimpleNamespace(async_entries=lambda d: [])
    for eid in cameras:
        hass.states.set(eid, "idle")
    return hass


async def _call(handler, hass, **msg):
    conn = _Conn()
    await handler(hass, conn, {"id": 9, **msg})
    return conn


def _safe(conn, code):
    (msg_id, got, message), = conn.errors
    assert (msg_id, got) == (9, code)
    assert message == "RuntimeError (details are in the Home Assistant log)"


# ── nova/camera_location ────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,indoor,outdoor", [
    ("outdoor", ["camera.hall*"], ["camera.porch*", "camera.garden"]),
    ("indoor", ["camera.hall*", "camera.garden"], ["camera.porch*"]),
    ("auto", ["camera.hall*"], ["camera.porch*"]),
])
async def test_location_pins_the_exact_camera_and_keeps_other_entries(cams, store,
                                                                       mode, indoor, outdoor):
    store.data.update(indoor_entities=["camera.hall*", "camera.garden"],
                      outdoor_entities=["camera.porch*", "camera.garden"])
    conn = await _call(cams.ws_camera_location, _hass("camera.garden"),
                       entity_id="camera.garden", mode=mode)
    assert conn.results == [(9, {"ok": True, "cameras": ["cams"]})]
    assert store.writes == [{"indoor_entities": indoor, "outdoor_entities": outdoor}]
    assert cams._logged[0][0] == "CONFIG" and mode.upper() in cams._logged[0][1]


@pytest.mark.parametrize("eid,cameras", [
    ("camera.gone", ()),                       # no such entity
    ("binary_sensor.door", ("binary_sensor.door",)),   # exists, not a camera
])
async def test_location_refuses_anything_but_a_live_camera(cams, store, eid, cameras):
    conn = await _call(cams.ws_camera_location, _hass(*cameras), entity_id=eid, mode="outdoor")
    assert conn.errors == [(9, "unknown_camera", eid)] and store.writes == []


async def test_current_behaviour_a_camera_location_change_writes_no_audit_row(cams, store, load,
                                                                              monkeypatch):
    # Looks wrong: marking an indoor camera outdoor takes its motion out of
    # the intrusion investigator. It is a safety change, but it leaves only a
    # CONFIG debug log line: no Action Audit Log row.
    al = load("action_log")
    rows = []
    for name in ("start", "start_many", "set_approval", "set_execution"):
        monkeypatch.setattr(al, name, lambda *a, **k: rows.append(a))
    await _call(cams.ws_camera_location, _hass("camera.hall"), entity_id="camera.hall",
                mode="outdoor")
    assert store.data["outdoor_entities"] == ["camera.hall"]
    assert rows == []


async def test_location_failure_returns_no_raw_text(cams, load, monkeypatch):
    def boom(*a):
        raise RuntimeError("token=abc")
    monkeypatch.setattr(load("outdoor"), "set_entity_location", boom)
    _safe(await _call(cams.ws_camera_location, _hass("camera.a"), entity_id="camera.a",
                      mode="indoor"), "camera_location_failed")


# ── nova/rename_camera ──────────────────────────────────────────────────────

async def test_rename_sets_a_trimmed_name_and_blank_reverts(cams, store, monkeypatch):
    monkeypatch.setattr(cams, "_get_camera_names", lambda: dict(store.data.get("camera_names", {})))
    hass = _hass("camera.porch")
    conn = await _call(cams.ws_rename_camera, hass, entity_id="camera.porch", name="  Porch  ")
    assert conn.results[0][1]["camera_names"] == {"camera.porch": "Porch"}
    assert "renamed to 'Porch' (Nova only)" in cams._logged[-1][1]
    conn = await _call(cams.ws_rename_camera, hass, entity_id="camera.porch", name=" ")
    assert conn.results[0][1]["camera_names"] == {}
    assert cams._logged[-1][1].endswith("name reverted (Nova only)")
    await _call(cams.ws_rename_camera, hass, entity_id="camera.porch", name=None)
    assert store.data["camera_names"] == {}


async def test_rename_refuses_anything_but_a_live_camera(cams, store):
    conn = await _call(cams.ws_rename_camera, _hass("light.porch"), entity_id="light.porch",
                       name="x")
    assert conn.errors == [(9, "unknown_camera", "light.porch")] and store.writes == []


async def test_current_behaviour_a_camera_name_has_no_length_limit(cams, store, monkeypatch):
    # Looks wrong: the name is stored as sent (only trimmed). It is shown on
    # the panel and can be spoken, and a 100,000 character name is accepted.
    monkeypatch.setattr(cams, "_get_camera_names", lambda: {})
    name = "N" * 100_000
    conn = await _call(cams.ws_rename_camera, _hass("camera.porch"), entity_id="camera.porch",
                       name=name)
    assert conn.errors == [] and store.data["camera_names"]["camera.porch"] == name


async def test_rename_failure_returns_no_raw_text(cams, store, monkeypatch):
    def boom():
        raise RuntimeError("token=abc")
    monkeypatch.setattr(cams, "_get_camera_names", boom)
    _safe(await _call(cams.ws_rename_camera, _hass("camera.a"), entity_id="camera.a", name="x"),
          "rename_failed")


# ── nova/camera_snapshot ────────────────────────────────────────────────────

@pytest.fixture
def frames(camera_mod, monkeypatch):
    cam = camera_mod
    st = types.SimpleNamespace(img=b"\xff\xd8jpeg", asked=[], error=None)

    async def best(hass, eid):
        st.asked.append(eid)
        if st.error:
            raise st.error
        return st.img
    monkeypatch.setattr(cam, "_get_best_image", best, raising=False)
    monkeypatch.setattr(cam, "_downscale_jpeg", lambda img, dim: img + b"@" + str(dim).encode(),
                        raising=False)
    return st


async def test_snapshot_returns_a_downscaled_base64_frame(cams, frames):
    conn = await _call(cams.ws_camera_snapshot, _hass("camera.porch"), entity_id="camera.porch")
    assert conn.results == [(9, {"image": base64.b64encode(b"\xff\xd8jpeg@960").decode()})]


async def test_snapshot_refuses_anything_but_a_live_camera(cams, frames):
    conn = await _call(cams.ws_camera_snapshot, _hass("lock.front"), entity_id="lock.front")
    assert conn.errors == [(9, "unknown_camera", "lock.front")] and frames.asked == []


async def test_snapshot_with_no_frame_is_none_and_logged_once(cams, frames):
    frames.img = None
    hass = _hass("camera.porch")
    for _ in range(3):
        conn = await _call(cams.ws_camera_snapshot, hass, entity_id="camera.porch")
        assert conn.results == [(9, {"image": None})]
    assert len(cams._logged) == 1 and cams._logged[0][0] == "CAMERA"


async def test_current_behaviour_a_snapshot_error_puts_raw_text_in_the_debug_log(cams, frames):
    # Looks wrong: the panel gets the safe type-only message, but the same
    # failure goes into the debug log with the raw exception text, and the
    # debug log is shown on the panel (nova/get_debug_log).
    frames.error = RuntimeError("http://user:pw@nvr.local/api token=abc")
    conn = await _call(cams.ws_camera_snapshot, _hass("camera.porch"), entity_id="camera.porch")
    _safe(conn, "snapshot_failed")
    assert "token=abc" in cams._logged[0][1]


# ── nova/camera_diagnostics ─────────────────────────────────────────────────

async def test_diagnostics_summarises_every_camera(cams, load, monkeypatch):
    hass = _hass("camera.a", "camera.b")
    conn = await _call(cams.ws_camera_diagnostics, hass)
    res = conn.results[0][1]
    assert [r["entity_id"] for r in res["summary"]] == ["camera.a", "camera.b"]
    assert res["probe"] is None and sum(res["platforms"].values()) == 2


async def test_diagnostics_probes_one_camera_and_logs_the_verdict(cams, camera_mod, monkeypatch):
    async def probe(hass, eid):
        return {"entity_id": eid, "verdict": "ok via frigate"}
    monkeypatch.setattr(camera_mod, "probe_camera", probe, raising=False)
    conn = await _call(cams.ws_camera_diagnostics, _hass("camera.a"), entity_id="camera.a")
    assert conn.results[0][1]["probe"]["verdict"] == "ok via frigate"
    assert cams._logged == [("CAMERA", "diag camera.a: ok via frigate")]


async def test_diagnostics_a_hung_probe_times_out(cams, camera_mod, monkeypatch):
    async def probe(hass, eid):
        await asyncio.sleep(3600)

    async def fast_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(camera_mod, "probe_camera", probe, raising=False)
    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
    conn = await _call(cams.ws_camera_diagnostics, _hass("camera.a"), entity_id="camera.a")
    assert conn.results[0][1]["probe"]["verdict"].startswith("probe timed out after 30s")


async def test_current_behaviour_diagnostics_probes_any_entity_id(cams, camera_mod, monkeypatch):
    # Looks wrong: unlike snapshot, rename and location, diagnostics does
    # not check the id is a camera (or exists) before probing it.
    probed = []

    async def probe(hass, eid):
        probed.append(eid)
        return {"entity_id": eid, "verdict": "no camera"}
    monkeypatch.setattr(camera_mod, "probe_camera", probe, raising=False)
    await _call(cams.ws_camera_diagnostics, _hass(), entity_id="lock.front_door")
    assert probed == ["lock.front_door"]


# ── nova/compute_camera_coverage and nova/mmwave_overview ──────────────────

async def test_current_behaviour_coverage_passes_the_panel_dict_on_unbounded(cams, load,
                                                                             monkeypatch):
    # Looks wrong: the camera dict from the panel goes to the reasoning LLM
    # prompt as sent, with no limit on its size or number of candidates.
    seen = []

    async def infer(hass, config, cam_ctx):
        seen.append(cam_ctx)
        return {"covered": ["hall"], "reason": "r", "source": "llm"}
    monkeypatch.setattr(load("camera_coverage"), "infer_coverage", infer)
    big = {"candidates": {f"room{i}": 0.5 for i in range(5000)}, "room": "x" * 50_000}
    conn = await _call(cams.ws_compute_camera_coverage, _hass(), camera=big)
    assert seen == [big] and conn.results[0][1]["covered"] == ["hall"]


async def test_coverage_failure_returns_no_raw_text(cams, load, monkeypatch):
    async def boom(*a):
        raise RuntimeError("token=abc")
    monkeypatch.setattr(load("camera_coverage"), "infer_coverage", boom)
    _safe(await _call(cams.ws_compute_camera_coverage, _hass(), camera={}), "coverage_failed")


async def test_mmwave_lists_only_rooms_with_presence_sensors(cams, load, monkeypatch):
    hass = _hass()
    hass.states.set("binary_sensor.k1", "on", friendly_name="Kitchen mmWave")
    hass.states.set("binary_sensor.k2", "off")
    ar = load("audio_routing")
    sensors = {"kitchen": ["binary_sensor.k1", "binary_sensor.k2", "binary_sensor.gone"],
               "hall": [], "den": ["binary_sensor.k2"]}
    monkeypatch.setattr(cams, "_all_areas_with_anything", lambda h: list(sensors))
    monkeypatch.setattr(ar, "presence_entities_in_area", lambda h, a: sensors[a])
    monkeypatch.setattr(cams, "_area_name", lambda h, a: a.title())
    monkeypatch.setattr(cams, "_is_outdoor_area", lambda h, a: False)
    conn = await _call(cams.ws_mmwave_overview, hass)
    res = conn.results[0][1]
    assert [r["area_id"] for r in res["rooms"]] == ["kitchen", "den"]
    assert res["summary"] == {"rooms_with_mmwave": 2, "rooms_detecting": 1, "total_sensors": 3}
    kitchen = res["rooms"][0]
    assert kitchen["state"] == "detecting" and kitchen["sensor_count"] == 2
    assert kitchen["sensors"][0]["name"] == "Kitchen mmWave"


async def test_mmwave_failure_returns_no_raw_text(cams, monkeypatch):
    def boom(h):
        raise RuntimeError("token=abc")
    monkeypatch.setattr(cams, "_all_areas_with_anything", boom)
    _safe(await _call(cams.ws_mmwave_overview, _hass()), "mmwave_overview_failed")
