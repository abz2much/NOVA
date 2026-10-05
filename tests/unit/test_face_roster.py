"""The resident roster, recent_faces and the Faces tab commands."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import stat
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeHass

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


@pytest.fixture
def fr(load, monkeypatch, tmp_path):
    mod = load("face_roster")
    load("recognition")
    path = tmp_path / "nova" / "face_roster.json"
    monkeypatch.setattr(mod, "_path", lambda: str(path))
    mod._NAMES.clear()
    yield mod
    mod._NAMES.clear()


@pytest.fixture
def rec(load, fr):
    r = load("recognition")
    for box in (r._FACE_LOG, r._PERSON_LOG, r._RECOGNITION_CACHE):
        box.clear()
    yield r
    for box in (r._FACE_LOG, r._PERSON_LOG, r._RECOGNITION_CACHE):
        box.clear()


class _Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _path(fr) -> pathlib.Path:
    return pathlib.Path(fr._path())


# ── roster ──────────────────────────────────────────────────────────────────

async def test_add_remove_case_and_spacing(fr):
    hass = _Hass()
    assert (await fr.async_add(hass, "Sam"))["added"] is True
    for same in ("sam", " Sam ", "SAM", "sam  "):
        res = await fr.async_add(hass, same)
        assert res["ok"] and res["added"] is False
    assert fr.names() == ["Sam"]
    assert fr.is_resident("sam") and fr.is_resident("  SAM ") and not fr.is_resident("Samuel")
    assert (await fr.async_add(hass, "Anna  Marie"))["added"] is True
    assert fr.is_resident("anna marie")
    res = await fr.async_remove(hass, " sam ")
    assert res["removed"] is True and fr.names() == ["Anna Marie"]
    assert (await fr.async_remove(hass, "nobody"))["removed"] is False


async def test_persists_across_a_restart(fr):
    hass = _Hass()
    await fr.async_add(hass, "Sam")
    await fr.async_add(hass, "Anna")
    assert json.loads(_path(fr).read_text()) == {"residents": ["Anna", "Sam"]}
    fr._NAMES.clear()                      # a restart
    assert fr.names() == []
    fr.load()
    assert fr.names() == ["Anna", "Sam"] and fr.is_resident("ANNA")
    await fr.async_remove(hass, "anna")
    fr._NAMES.clear()
    fr.load()
    assert fr.names() == ["Sam"]


async def test_file_is_private_to_the_ha_user(fr):
    await fr.async_add(_Hass(), "Sam")
    assert stat.S_IMODE(os.stat(_path(fr)).st_mode) == 0o600
    os.chmod(_path(fr), 0o644)             # something loosened it
    await fr.async_add(_Hass(), "Anna")
    assert stat.S_IMODE(os.stat(_path(fr)).st_mode) == 0o600   # an atomic write sets it again


def test_missing_empty_or_corrupt_file_never_raises(fr):
    fr.load()                                                  # missing
    assert fr.names() == []
    _path(fr).parent.mkdir(parents=True)
    for content in ("", "   ", "{not json", "[]", "null", '"x"', '{"residents": "Sam"}',
                    '{"residents": [1, null, "", "unknown", "a\\u0000b"]}'):
        _path(fr).write_text(content)
        fr._NAMES["x"] = "x"
        fr.load()
        assert fr.names() in ([], ["x"]), content


def test_a_bad_entry_in_a_good_file_is_dropped_and_the_roster_capped(fr):
    _path(fr).parent.mkdir(parents=True)
    many = [f"Person {i}" for i in range(fr.MAX_RESIDENTS + 10)]
    _path(fr).write_text(json.dumps({"residents": ["Sam", "sam", "Unknown", 5, *many]}))
    fr.load()
    assert "Sam" in fr.names() and "Unknown" not in fr.names()
    assert len(fr.names()) == fr.MAX_RESIDENTS


@pytest.mark.parametrize("bad", [
    5, None, True, ["Sam"], b"Sam", "", "   ", "x" * 61, "Sa\x00m", "Sam\n\x07", "Sam‮",
    "unknown", "Unknown", " UNKNOWN PERSON ", "none", "null", "unknown_face"])
async def test_bad_names_are_refused(fr, bad):
    res = await fr.async_add(_Hass(), bad)
    assert res["ok"] is False and res["error"] == "invalid_name"
    assert fr.names() == [] and not _path(fr).exists()


def test_good_names(fr):
    assert fr.clean_name("  Anna   Marie ") == "Anna Marie"
    assert fr.clean_name("Zoë O'Brien-Smith") == "Zoë O'Brien-Smith"
    assert fr.clean_name("x" * 60) == "x" * 60


async def test_roster_full(fr):
    hass = _Hass()
    for i in range(fr.MAX_RESIDENTS):
        assert (await fr.async_add(hass, f"P{i}"))["ok"]
    res = await fr.async_add(hass, "One more")
    assert res["ok"] is False and res["error"] == "roster_full"


async def test_never_raises(fr, monkeypatch):
    class _Broken:
        async def async_add_executor_job(self, *a):
            raise RuntimeError("executor gone")
    res = await fr.async_add(_Broken(), "Sam")
    assert res["ok"] is True and res["error"] == "not_saved" and fr.is_resident("Sam")
    assert (await fr.async_remove(_Broken(), "Sam"))["error"] == "not_saved"
    assert fr.is_resident(None) is False and fr.is_resident(5) is False
    monkeypatch.setattr(fr, "_normalize", lambda n: 1 / 0)
    assert fr.is_resident("Sam") is False
    assert (await fr.async_add(_Hass(), "Anna"))["ok"] is False
    assert (await fr.async_remove(_Hass(), "Anna"))["ok"] is False


async def test_concurrent_saves_keep_the_newest_state(fr):
    class _Slow(_Hass):
        async def async_add_executor_job(self, func, *args):
            await asyncio.sleep(0)
            return func(*args)
    hass = _Slow()
    await asyncio.gather(fr.async_add(hass, "A"), fr.async_add(hass, "B"), fr.async_add(hass, "C"))
    assert json.loads(_path(fr).read_text())["residents"] == ["A", "B", "C"]


def test_loaded_at_setup_never_at_import():
    src = (COMP / "face_roster.py").read_text()
    top = [n for n in ast.parse(src).body if isinstance(n, ast.Expr)]
    assert not any("load(" in ast.unparse(n) for n in top)
    tree = ast.parse((COMP / "__init__.py").read_text())
    setup = next(n for n in tree.body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup")
    stmts = [ast.unparse(s) for s in setup.body]
    cfg = stmts.index("paths.configure(hass)")
    idx = next(i for i, s in enumerate(stmts) if "face_roster.load" in s)
    assert idx > cfg and "async_add_executor_job" in stmts[idx]


# ── recent_faces ────────────────────────────────────────────────────────────

class _Sensors:
    """A hass with Frigate last recognised face sensors."""
    def __init__(self, sensors, mqtt=True):
        self._sensors = sensors
        self._mqtt = mqtt
        outer = self

        class _States:
            def async_all(self, domain=None):
                return outer._sensors if domain in (None, "sensor") else []
        self.states = _States()

        class _Services:
            def has_service(self, d, s):
                return outer._mqtt
        self.services = _Services()


def _sensor(slug, name, score=0.9, age_s=30):
    when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    from fakes import FakeState
    return FakeState(f"sensor.{slug}_last_recognized_face", name, {"score": score},
                     last_changed=when, last_updated=when)


async def test_recent_faces_merges_both_sources_and_dedupes(rec, fr):
    await fr.async_add(_Hass(), "Sam")
    rec.remember_recognition("front", "Sam", 91.0, source="doubletake")
    rec.remember_recognition("front", "sam", 95.0, source="frigate")    # same person and camera
    rec.remember_recognition("garden", "Stranger", 20.0, source="frigate")
    rec.remember_recognition("garden", "unknown", 0.0, source="doubletake")
    hass = _Sensors([_sensor("hall", "Anna", 0.88, age_s=500),
                     _sensor("front", "Sam", 0.7, age_s=3000),
                     _sensor("porch", "none")])
    rows = rec.recent_faces(hass)
    keyed = {(r["name"].lower(), r["camera"]): r for r in rows}
    assert ("sam", "front") in keyed and len([r for r in rows if r["camera"] == "front"]) == 1
    assert keyed[("sam", "front")]["age_seconds"] < 60                     # the newest sighting wins
    assert keyed[("anna", "hall")]["source"] == "frigate_sensor"
    assert keyed[("anna", "hall")]["confidence"] == 88.0
    assert ("none", "porch") not in keyed
    ages = [r["age_seconds"] for r in rows]
    assert ages == sorted(ages)                                             # newest first


async def test_recent_faces_known_unknown_and_roster_flag(rec, fr):
    await fr.async_add(_Hass(), "Sam")
    rec.remember_recognition("front", "Sam", 91.0)
    rec.remember_recognition("front", "Visitor", 80.0)
    rec.remember_recognition("garden", "unknown", 40.0)
    by = {r["name"]: r for r in rec.recent_faces(_Sensors([]))}
    assert by["Sam"]["known"] is True and by["Sam"]["resident"] is True
    assert by["Visitor"]["known"] is True and by["Visitor"]["resident"] is False
    assert by["unknown"]["known"] is False and by["unknown"]["resident"] is False


def test_recent_faces_has_no_images_and_the_exact_keys(rec):
    rec.remember_recognition("front", "Sam", 91.0)
    row = rec.recent_faces(_Sensors([]))[0]
    assert set(row) == {"name", "camera", "camera_entity", "confidence", "age_seconds",
                        "known", "resident", "source"}
    assert not any("image" in k or "snapshot" in k or "b64" in k for k in row)
    src = (COMP / "recognition.py").read_text()
    body = src.split("def recent_faces")[1].split("def source_status")[0]
    assert "image" not in body.lower().replace("no image", "")


def test_recent_faces_empty_and_limit(rec):
    assert rec.recent_faces(_Sensors([])) == []
    for i in range(30):
        rec.remember_recognition(f"cam{i}", f"Person {i}", 80.0)
    assert len(rec.recent_faces(_Sensors([]), limit=5)) == 5
    assert len(rec.recent_faces(_Sensors([]), limit="x")) == 20
    assert len(rec.recent_faces(_Sensors([]), limit=999)) == 30
    assert len(rec.recent_faces(_Sensors([]), limit=-4)) == 1


def test_recent_faces_never_raises(rec):
    class _Boom:
        class states:
            @staticmethod
            def async_all(domain=None):
                raise RuntimeError("no states")
    assert rec.recent_faces(_Boom()) == []
    assert rec.recent_faces(None) == []
    assert rec.source_status(None)["configured"] is True or True      # does not raise


def test_source_status_empty_state(rec):
    st = rec.source_status(_Sensors([], mqtt=False))
    assert st["configured"] is False and st["frigate_sensors"] == 0
    st = rec.source_status(_Sensors([_sensor("hall", "Anna")], mqtt=False))
    assert st["configured"] is True and st["frigate_sensors"] == 1
    assert rec.source_status(_Sensors([], mqtt=True))["configured"] is True


def test_remember_recognition_still_feeds_the_cache_and_who_is_where(rec):
    rec.remember_recognition("front", "Sam", 91.0)
    assert rec.who_is_where(None) == {"camera.front": "Sam"}
    assert rec.last_seen_at(None, "camera.front")["name"] == "Sam"


# ── commands ────────────────────────────────────────────────────────────────

def test_three_commands_are_admin_gated_and_registered():
    src = (COMP / "ws_faces.py").read_text()
    assert src.count("@websocket_api.require_admin\n@websocket_api.websocket_command({") == 3
    ws = (COMP / "websocket.py").read_text()
    for fn in ("ws_list_faces", "ws_add_resident", "ws_remove_resident"):
        assert f"websocket_api.async_register_command(hass, {fn})" in ws
    assert "from .ws_faces import" in ws


def test_ws_sources_reads_the_new_file():
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from ws_sources import ws_paths, ws_text
    assert any(p.name == "ws_faces.py" for p in ws_paths())
    assert '"nova/list_faces"' in ws_text()
