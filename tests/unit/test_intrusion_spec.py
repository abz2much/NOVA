"""Intrusion snapshots, call-off and learning checked against their spec (7d).

These pin the details the older intrusion tests leave open: the shape of each
record, which labels learning counts, the call-off's labelling and decision
outcome, and the snapshot housekeeping. The safety rule stands throughout:
learning only ever damps the weak alerts."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import types

import pytest


@pytest.fixture
def intr(load, tmp_path, monkeypatch):
    m = load("intrusion")
    monkeypatch.setattr(m, "SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setattr(m, "LOG_PATH", pathlib.Path(tmp_path / "log" / "intrusion_log.json"))
    monkeypatch.setattr(m, "_log", [])
    monkeypatch.setattr(m, "_log_loaded", True)
    monkeypatch.setattr(m, "_called_off_until", 0.0)
    monkeypatch.setattr(m, "_acknowledged_ts", 0.0)
    monkeypatch.setattr(m, "_false_alarms", [])
    monkeypatch.setattr(m, "_last_snapshot", {})
    monkeypatch.setattr(m, "_last_decision_id", None)
    return m


@pytest.fixture
def outcomes(monkeypatch):
    calls = []
    fake = types.ModuleType("jc.decision_record")
    fake.OUTCOME_WRONG, fake.OUTCOME_GOOD, fake.SOURCE_PANEL_LABEL = "wrong", "good", "panel_label"
    fake.result = True

    def set_outcome(record_id, verdict, source=""):
        calls.append(("exact", record_id, verdict, source))
        return fake.result

    def set_outcome_recent(kind, verdict, source="", max_age=3600.0):
        calls.append(("recent", kind, verdict, source, max_age))
        return True
    fake.set_outcome, fake.set_outcome_recent = set_outcome, set_outcome_recent
    monkeypatch.setitem(sys.modules, "jc.decision_record", fake)
    monkeypatch.setattr(sys.modules["jc"], "decision_record", fake, raising=False)
    fake.calls = calls
    return fake


# ── acknowledge and call-off ────────────────────────────────────────────────

def test_acknowledge_reports_the_hold(intr):
    assert intr.acknowledge() == {"ok": True, "held_seconds": 300}
    assert intr.is_acknowledged() is True and intr.is_called_off() is False


def test_a_call_off_records_the_last_camera_and_reason(intr, outcomes, monkeypatch):
    monkeypatch.setattr(intr.time, "time", lambda: 1000.4)
    intr._last_snapshot = {"camera": "camera.yard", "path": "p"}
    out = intr.dismiss_intrusion(None)
    assert out == {"ok": True, "suppressed_seconds": 600,
                   "recorded": {"ts": 1000, "reason": "", "camera": "camera.yard"}}
    assert intr._called_off_until == pytest.approx(1600.4)
    assert intr._false_alarms == [out["recorded"]]
    assert outcomes.calls == [("recent", "intrusion", "wrong", "dismiss_intrusion", 7200.0)]


def test_the_false_alarm_list_keeps_the_newest_fifty(intr, outcomes):
    for i in range(53):
        intr.dismiss_intrusion(str(i))
    assert [r["reason"] for r in intr._false_alarms] == [str(i) for i in range(3, 53)]


def test_an_exact_record_falls_back_when_already_judged(intr, outcomes):
    outcomes.result = False
    intr.set_last_decision_id("17")
    intr.dismiss_intrusion("x")
    assert outcomes.calls == [("exact", 17, "wrong", "dismiss_intrusion"),
                              ("recent", "intrusion", "wrong", "dismiss_intrusion", 7200.0)]
    assert intr._last_decision_id is None


@pytest.mark.parametrize("value", [None, "abc", object()])
def test_a_bad_decision_id_is_ignored(intr, value):
    intr.set_last_decision_id(5)
    intr.set_last_decision_id(value)
    assert intr._last_decision_id == 5


def test_a_call_off_labels_the_first_alert_of_its_decision(intr, outcomes):
    mine = intr.record_event("investigating", decision_id=9)
    other = intr.record_event("investigating", decision_id=8)
    intr.set_last_decision_id(9)
    intr.dismiss_intrusion("cat")
    assert (mine["label"], mine["label_source"]) == ("false", "dismiss_intrusion")
    assert other["label"] is None
    assert json.loads(intr.LOG_PATH.read_text())[0]["label"] == "false"


def test_the_call_off_label_rules(intr):
    now = time.time()
    old = intr.record_event("investigating")
    old["ts"] = now - 7300
    confirmed = intr.record_event("confirmed")
    first = intr.record_event("investigating", decision_id=1)
    latest = intr.record_event("investigating")
    # No match on the decision: the latest first alert in the window.
    assert intr._label_call_off(42, now) == latest["id"]
    assert first["label"] is None and confirmed["label"] is None and old["label"] is None
    # The newest candidate already labelled: nothing is relabelled.
    assert intr._label_call_off(None, now) is None
    assert first["label"] is None and latest["labeled_ts"] == now


def test_a_given_label_is_never_overwritten(intr):
    ev = intr.record_event("investigating", decision_id=3)
    intr.label_event(ev["id"], "real")
    assert intr._label_call_off(3, time.time()) is None
    assert ev["label"] == "real"


def test_status_counts_down(intr, outcomes, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(intr.time, "time", lambda: clock[0])
    intr.dismiss_intrusion()
    clock[0] += 100.7
    st = intr.status()
    assert st == {"last_snapshot": None, "called_off": True, "acknowledged": False,
                  "suppressed_for": 499, "false_alarms_24h": 1}
    clock[0] += 1000
    assert intr.status()["suppressed_for"] == 0


# ── the event log ───────────────────────────────────────────────────────────

def test_a_recorded_event_has_every_field(intr, monkeypatch):
    monkeypatch.setattr(intr.time, "time", lambda: 1234.9)
    ev = intr.record_event("", reason=None, snapshot={"camera": "camera.c", "path": "/p.jpg"},
                           zones=("a", "b"), max_depth=2, decision_id=7)
    assert ev == {"id": "evt_1234_0", "ts": 1234.9, "kind": "investigating", "reason": "",
                  "breach": "", "breach_area": "", "camera": "camera.c",
                  "snapshot_path": "/p.jpg", "zones": ["a", "b"], "max_depth": 2,
                  "label": None, "pattern": intr._pattern_key(None, None, 1234.9),
                  "decision_id": 7}


def test_the_pattern_is_place_and_three_hour_slot(intr):
    noon = time.mktime((2026, 6, 1, 13, 0, 0, 0, 0, -1))
    assert intr._pattern_key(" Kitchen ", "camera.x", noon) == "kitchen|4"
    assert intr._pattern_key("", "Camera.Y", noon) == "camera.y|4"
    assert intr._pattern_key(None, None, noon) == "unknown|4"


@pytest.mark.parametrize("limit, count", [(0, 1), (-5, 1), ("3", 3), ("x", 50), (None, 50),
                                          (999, 60)])
def test_get_log_limits(intr, limit, count):
    for i in range(60):
        intr.record_event("investigating", reason=str(i))
    out = intr.get_log(limit)
    assert len(out) == count and out[0]["reason"] == "59"


def test_the_log_loads_once_and_keeps_the_newest(intr, tmp_path):
    intr.LOG_PATH.parent.mkdir(parents=True)
    intr.LOG_PATH.write_text(json.dumps([{"id": str(i)} for i in range(205)]))
    intr._log_loaded = False
    loaded = intr._load_log()
    assert len(loaded) == 200 and loaded[0]["id"] == "5"
    intr.LOG_PATH.write_text("[]")
    assert intr._load_log() is loaded


@pytest.mark.parametrize("text", ["{}", "not json"])
def test_a_bad_log_file_is_an_empty_log(intr, text):
    intr.LOG_PATH.parent.mkdir(parents=True)
    intr.LOG_PATH.write_text(text)
    intr._log_loaded = False
    assert intr._load_log() == []


def test_a_missing_log_file_is_an_empty_log(intr):
    intr._log_loaded = False
    assert intr._load_log() == [] and intr._log_loaded is True


def test_a_panel_label_sets_the_decision_outcome(intr, outcomes):
    ev = intr.record_event("investigating", decision_id="12")
    assert intr.label_event(ev["id"], "false") == {"ok": True, "id": ev["id"], "label": "false"}
    intr.label_event(ev["id"], "real")
    assert outcomes.calls == [("exact", 12, "wrong", "panel_label"),
                              ("exact", 12, "good", "panel_label")]
    intr.label_event(ev["id"], "")
    assert ev["label"] is None and ev["label_source"] is None and "labeled_ts" in ev
    intr.label_event(ev["id"], "real", source="other")
    assert ev["label_source"] == "other" and len(outcomes.calls) == 2


def test_a_label_without_a_decision_sets_no_outcome(intr, outcomes):
    ev = intr.record_event("investigating")
    intr.label_event(ev["id"], "false")
    assert outcomes.calls == []


# ── learning ────────────────────────────────────────────────────────────────

def _labelled(intr, label, source="panel_label", area="hall"):
    ev = intr.record_event("investigating", breach_area=area)
    ev["label"] = label
    ev["label_source"] = source
    return ev


def test_only_labels_a_person_gave_are_learned(intr):
    for _ in range(3):
        _labelled(intr, "false", source="automatic")
    verdict = intr.pattern_verdict("hall", None)
    assert (verdict["false_count"], verdict["damp"]) == (0, False)
    _labelled(intr, "false", source=None)        # an old label: from the panel
    _labelled(intr, "false", source="dismiss_intrusion")
    _labelled(intr, "false")
    verdict = intr.pattern_verdict("hall", None)
    assert verdict["false_count"] == 3 and verdict["damp"] is True
    assert verdict["pattern"] == intr._pattern_key("hall", None)


def test_damping_reads_the_pattern_of_now(intr, monkeypatch):
    for _ in range(3):
        _labelled(intr, "false")
    assert intr.should_damp_weak_alert("hall", None) is True
    assert intr.should_damp_weak_alert("garden", None) is False


def test_the_summary_counts_every_label(intr):
    for _ in range(3):
        _labelled(intr, "false", source="automatic")
    _labelled(intr, "real", area="porch")
    intr.record_event("confirmed", breach_area="porch")
    summary = intr.learning_summary()
    hall, porch = intr._pattern_key("hall", None), intr._pattern_key("porch", None)
    assert summary == {"events": 5, "labeled": 4,
                       "patterns": {hall: {"false": 3, "real": 0}, porch: {"false": 0, "real": 1}},
                       "damped_patterns": [hall], "min_false_to_damp": 3}


# ── snapshots ───────────────────────────────────────────────────────────────

class _Hass:
    def __init__(self, media=None):
        self.config = types.SimpleNamespace(media_dirs=media)

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


async def test_a_snapshot_is_named_by_tag_camera_and_time(intr, monkeypatch):
    monkeypatch.setattr(intr.time, "time", lambda: 77.8)
    cam = types.ModuleType("homeassistant.components.camera")

    async def get_image(hass, entity, timeout=10):
        assert timeout == 10
        return types.SimpleNamespace(content=b"jpg")
    cam.async_get_image = get_image
    monkeypatch.setitem(sys.modules, "homeassistant.components.camera", cam)
    info = await intr.capture_snapshot(_Hass(), "camera.front.door", tag="face")
    assert info == {"path": os.path.join(intr.SNAPSHOT_DIR, "face_front.door_77.jpg"),
                    "camera": "camera.front.door", "ts": 77}
    assert intr.last_snapshot() == info


async def test_an_empty_image_is_no_snapshot(intr, monkeypatch):
    cam = types.ModuleType("homeassistant.components.camera")

    async def get_image(hass, entity, timeout=10):
        return types.SimpleNamespace(content=b"")
    cam.async_get_image = get_image
    monkeypatch.setitem(sys.modules, "homeassistant.components.camera", cam)
    assert await intr.capture_snapshot(_Hass(), "camera.x") is None
    assert intr.last_snapshot() is None


def test_pruning_keeps_the_newest_forty(intr):
    os.makedirs(intr.SNAPSHOT_DIR)
    for i in range(43):
        path = os.path.join(intr.SNAPSHOT_DIR, f"s{i}.jpg")
        open(path, "wb").close()
        os.utime(path, (i, i))
    open(os.path.join(intr.SNAPSHOT_DIR, "keep.txt"), "w").close()
    intr._prune_old()
    left = sorted(os.listdir(intr.SNAPSHOT_DIR))
    assert len(left) == 41 and "keep.txt" in left
    assert not {"s0.jpg", "s1.jpg", "s2.jpg"} & set(left)


def test_pruning_a_missing_dir_is_quiet(intr):
    intr._prune_old()


def test_a_read_of_an_empty_path_is_none(intr):
    assert intr._read_b64("") is None


@pytest.mark.parametrize("raw, minutes", [(None, 60.0), (0, 1.0), ("15", 15.0), ("x", 60.0)])
def test_the_notification_image_lifetime(intr, load, monkeypatch, raw, minutes):
    nc = load("nova_config")
    monkeypatch.setattr(nc, "get", lambda k, d=None: d if raw is None else raw)
    assert intr._notify_ttl_minutes(None) == minutes


def test_the_notification_copy(intr, tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"data")
    name = intr._copy_for_notify(str(src), str(tmp_path / "out"))
    assert name.endswith(".jpg") and (tmp_path / "out" / name).read_bytes() == b"data"
    assert intr._copy_for_notify(str(tmp_path / "missing.jpg"), str(tmp_path / "out")) is None
    intr._delete_notify_copy(str(tmp_path / "out" / name))
    intr._delete_notify_copy(str(tmp_path / "out" / name))
    assert not (tmp_path / "out" / name).exists()


async def test_another_media_dir_is_used_when_there_is_no_local(intr, tmp_path, monkeypatch):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"data")
    auth = types.ModuleType("homeassistant.components.http.auth")
    auth.async_sign_path = lambda hass, path, expiry: f"{path}?s={expiry.total_seconds()}"
    event = types.ModuleType("homeassistant.helpers.event")
    later = []
    event.async_call_later = lambda hass, delay, cb: later.append((delay, cb))
    monkeypatch.setitem(sys.modules, "homeassistant.components.http.auth", auth)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", event)
    url = await intr.get_notification_image_url(_Hass({"usb": str(tmp_path / "m")}), str(src))
    assert url.startswith("/media/usb/nova_intrusion/") and url.endswith("?s=3600.0")
    assert later[0][0] == 3600.0


async def test_a_signing_failure_removes_the_copy(intr, tmp_path, monkeypatch):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"data")
    auth = types.ModuleType("homeassistant.components.http.auth")

    def sign(hass, path, expiry):
        raise RuntimeError
    auth.async_sign_path = sign
    monkeypatch.setitem(sys.modules, "homeassistant.components.http.auth", auth)
    media = tmp_path / "m"
    assert await intr.get_notification_image_url(_Hass({"local": str(media)}), str(src)) is None
    assert os.listdir(media / "nova_intrusion") == []


async def test_a_scheduling_failure_removes_the_copy(intr, tmp_path, monkeypatch):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"data")
    auth = types.ModuleType("homeassistant.components.http.auth")
    auth.async_sign_path = lambda hass, path, expiry: path
    event = types.ModuleType("homeassistant.helpers.event")

    def later(hass, delay, cb):
        raise RuntimeError
    event.async_call_later = later
    monkeypatch.setitem(sys.modules, "homeassistant.components.http.auth", auth)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", event)
    media = tmp_path / "m"
    assert await intr.get_notification_image_url(_Hass({"local": str(media)}), str(src)) is None
    assert os.listdir(media / "nova_intrusion") == []


async def test_a_failed_copy_gives_no_url(intr, tmp_path):
    hass = _Hass({"local": str(tmp_path / "m")})
    assert await intr.get_notification_image_url(hass, str(tmp_path / "missing.jpg")) is None


def test_migration_leaves_other_files_and_unmatched_log_entries(intr, tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "a.jpg").write_bytes(b"a")
    (legacy / "notes.txt").write_text("n")
    monkeypatch.setattr(intr, "_LEGACY_SNAPSHOT_DIR", str(legacy))
    intr._log.extend([{"snapshot_path": str(legacy / "a.jpg")}, {"snapshot_path": "/other.jpg"},
                      {}])
    assert intr.migrate_legacy_snapshots() == {"moved": 1, "skipped": 0}
    assert (legacy / "notes.txt").exists() and not (legacy / "a.jpg").exists()
    assert intr._log[0]["snapshot_path"] == os.path.join(intr.SNAPSHOT_DIR, "a.jpg")
    assert intr._log[1]["snapshot_path"] == "/other.jpg"
    saved = json.loads(intr.LOG_PATH.read_text())
    assert saved[0]["snapshot_path"] == os.path.join(intr.SNAPSHOT_DIR, "a.jpg")
