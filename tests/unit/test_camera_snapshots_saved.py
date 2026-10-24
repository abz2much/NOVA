"""Camera snapshots survive a restart (8.70.1): saved on each new one,
loaded at start, newest 20 kept whatever their age, a broken file or
entry ignored."""
import json
import time
import types

import pytest


class _Hass:
    """Runs executor jobs straight away."""

    def async_add_executor_job(self, func, *args):
        func(*args)
        return types.SimpleNamespace(add_done_callback=lambda cb: None)


@pytest.fixture
def pb(load, monkeypatch, tmp_path):
    mod = load("proactive_briefing")
    ns = load("nova_state")
    path = tmp_path / "camera_snapshots.json"
    monkeypatch.setattr(mod, "_snapshots_path", lambda: str(path))
    monkeypatch.setattr(ns, "_HASS", {"hass": _Hass()})
    monkeypatch.setattr(ns, "notify", lambda: None)
    mod._SNAPSHOTS.clear()
    mod._WRITES.update(queued=0, saved=0)
    mod.path = path
    yield mod
    mod._SNAPSHOTS.clear()


def test_snapshots_survive_a_restart(pb):
    pb.record_snapshot("Doorbell", "camera.doorbell", "A parcel on the step.", "doorbell")
    pb.record_snapshot("Drive", "camera.drive", "A car pulled in.", "vehicle", "medium")
    pb._SNAPSHOTS.clear()
    pb.load_snapshots()
    snaps = list(pb._SNAPSHOTS)
    assert [s.analysis for s in snaps] == ["A parcel on the step.", "A car pulled in."]
    assert snaps[1].camera_entity == "camera.drive" and snaps[1].urgency == "medium"


def test_old_snapshots_are_kept(pb):
    week_ago = time.time() - 7 * 86400
    pb.path.write_text(json.dumps([{
        "timestamp": week_ago, "camera_name": "Garden", "camera_entity": "camera.garden",
        "analysis": "A fox.", "detection_type": "motion", "urgency": "low"}]))
    pb.load_snapshots()
    assert [s.analysis for s in pb._SNAPSHOTS] == ["A fox."]


def test_only_the_newest_twenty_are_kept(pb):
    for i in range(25):
        pb.record_snapshot("Cam", "camera.c", f"seen {i}")
    assert len(pb._SNAPSHOTS) == pb.MAX_SNAPSHOTS == 20
    pb._SNAPSHOTS.clear()
    pb.load_snapshots()
    assert [s.analysis for s in pb._SNAPSHOTS] == [f"seen {i}" for i in range(5, 25)]


def test_load_merges_with_snapshots_recorded_since_start(pb):
    pb.record_snapshot("Cam", "camera.c", "before restart")
    pb._SNAPSHOTS.clear()
    pb._SNAPSHOTS.append(pb.CameraSnapshot(time.time() + 1, "Cam", "camera.c",
                                           "after restart", "motion"))
    pb.load_snapshots()
    pb.load_snapshots()   # loading twice adds nothing
    assert [s.analysis for s in pb._SNAPSHOTS] == ["before restart", "after restart"]


@pytest.mark.parametrize("content", [None, "", "{not json", '{"a": 1}'])
def test_missing_or_broken_file_is_ignored(pb, content):
    if content is not None:
        pb.path.write_text(content)
    pb.load_snapshots()
    assert list(pb._SNAPSHOTS) == []


def test_bad_entries_are_skipped(pb):
    good = {"timestamp": time.time(), "camera_name": "Cam", "camera_entity": "camera.c",
            "analysis": "ok", "detection_type": "motion", "urgency": "low"}
    pb.path.write_text(json.dumps([
        "nope", {**good, "timestamp": "yesterday"}, {**good, "timestamp": True},
        {k: v for k, v in good.items() if k != "analysis"}, {**good, "urgency": 3}, good]))
    pb.load_snapshots()
    assert [s.analysis for s in pb._SNAPSHOTS] == ["ok"]


def test_nothing_saved_before_nova_is_set_up(pb, load, monkeypatch):
    monkeypatch.setattr(load("nova_state"), "_HASS", {"hass": None})
    pb.record_snapshot("Cam", "camera.c", "seen")
    assert not pb.path.exists()


def test_an_older_write_never_replaces_a_newer_one(pb):
    pb._write_snapshots(2, [{"n": 2}])
    pb._write_snapshots(1, [{"n": 1}])
    assert json.loads(pb.path.read_text()) == [{"n": 2}]
