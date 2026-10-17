"""The recognition log survives a restart (8.58.7): the Faces tab is not empty
after Nova restarts. Names, cameras, scores and times only."""
import json
from datetime import timedelta

import pytest


@pytest.fixture
def rec(load, tmp_path, monkeypatch):
    r = load("recognition")
    monkeypatch.setattr(r, "_log_path", lambda: str(tmp_path / "recent_faces.json"))
    r._FACE_LOG.clear()
    r._RECOGNITION_CACHE.clear()
    monkeypatch.setattr(r, "_HASS", None)
    yield r
    r._FACE_LOG.clear()
    r._RECOGNITION_CACHE.clear()


def _save(r):
    r._write_log([r._stamp_out(e) for e in list(r._FACE_LOG)])


def test_a_saved_sighting_comes_back(rec):
    rec.remember_recognition("front_door", "Sam", 91.5, source="frigate")
    _save(rec)
    rec._FACE_LOG.clear()
    rec._RECOGNITION_CACHE.clear()
    back = rec._read_log()
    assert [(e["name"], e["camera_entity"], e["confidence"]) for e in back] == [
        ("Sam", "camera.front_door", 91.5)]
    assert abs((back[0]["ts"] - rec._utcnow()).total_seconds()) < 5


async def test_restore_fills_the_log_and_cache(rec, fake_hass):
    rec.remember_recognition("hall", "Sam", 80.0, source="doubletake")
    _save(rec)
    rec._FACE_LOG.clear()
    rec._RECOGNITION_CACHE.clear()

    async def run(fn, *a):
        return fn(*a)
    fake_hass.async_add_executor_job = run
    await rec.restore(fake_hass)
    assert len(rec._FACE_LOG) == 1
    assert rec._RECOGNITION_CACHE["camera.hall"]["name"] == "Sam"
    assert rec.recent_faces(fake_hass)[0]["name"] == "Sam"


def test_old_future_and_odd_rows_are_dropped(rec, tmp_path):
    now = rec._utcnow()
    rows = [
        {"camera_entity": "camera.a", "name": "Old", "confidence": 90,
         "ts": (now - timedelta(days=2)).isoformat() + "+00:00"},
        {"camera_entity": "camera.a", "name": "Future", "confidence": 90,
         "ts": (now + timedelta(hours=1)).isoformat() + "+00:00"},
        {"camera_entity": "light.a", "name": "NotCamera", "confidence": 90,
         "ts": now.isoformat() + "+00:00"},
        {"camera_entity": "camera.a", "name": "", "confidence": 90,
         "ts": now.isoformat() + "+00:00"},
        {"camera_entity": "camera.a", "name": "NoTime"},
        "junk",
        {"camera_entity": "camera.a", "name": "Fine", "confidence": 70,
         "ts": now.isoformat() + "+00:00"},
    ]
    (tmp_path / "recent_faces.json").write_text(json.dumps({"faces": rows}))
    assert [e["name"] for e in rec._read_log()] == ["Fine"]


def test_a_missing_or_corrupt_file_gives_nothing(rec, tmp_path):
    assert rec._read_log() == []
    (tmp_path / "recent_faces.json").write_text("{not json")
    assert rec._read_log() == []


def test_nothing_is_scheduled_before_restore(rec):
    rec.remember_recognition("hall", "Sam", 80.0)
    assert rec._SAVE_HANDLE is None


def test_the_file_holds_no_image_fields(rec, tmp_path):
    rec.remember_recognition("hall", "Sam", 80.0, source="frigate")
    _save(rec)
    row = json.loads((tmp_path / "recent_faces.json").read_text())["faces"][0]
    assert set(row) == {"camera_entity", "name", "confidence", "source", "ts"}
