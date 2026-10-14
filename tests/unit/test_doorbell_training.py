"""Fresh behaviour tests for doorbell_training.py, written from its spec.

The doorbell log is a JSON Lines file of analysed doorbell events. Nova adds
to it as presses are analysed, reads it back for the panel, mines it for
time-of-day visitor patterns, and can fill it from the Nest doorbell's
recorded history. Nothing here may raise into a caller.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def dbt(load, tmp_path, monkeypatch):
    mod = load("doorbell_training")
    monkeypatch.setattr(mod, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(mod, "LOG_PATH", None)
    return mod


def _path(dbt):
    return dbt._log_path()


def _rows(dbt, rows):
    import os
    os.makedirs(dbt._log_dir(), exist_ok=True)
    with open(_path(dbt), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def _ago(days, hour, minute=0):
    dt = (datetime.utcnow() - timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return dt.isoformat() + "Z"


def _ev(ts, camera="Porch", category="delivery", **kw):
    return {"ts": ts, "camera": camera, "entity_id": "camera.porch",
            "image_source": "live", "category": category, **kw}


# ── where the log lives ─────────────────────────────────────────────────────

def test_the_log_lives_in_the_nova_folder_by_default(load, monkeypatch):
    mod = load("doorbell_training")
    paths = load("paths")
    monkeypatch.setattr(mod, "LOG_DIR", None)
    monkeypatch.setattr(mod, "LOG_PATH", None)
    monkeypatch.setattr(paths, "nova_path", lambda *parts: "/data/nova")
    assert mod._log_dir() == "/data/nova"
    assert mod._log_path() == "/data/nova/doorbell_log.jsonl"


def test_overrides_win(dbt, monkeypatch, tmp_path):
    monkeypatch.setattr(dbt, "LOG_PATH", str(tmp_path / "x.jsonl"))
    assert dbt._log_path() == str(tmp_path / "x.jsonl")
    assert dbt.MAX_RECORDS == 5000


# ── writing ─────────────────────────────────────────────────────────────────

def test_an_event_is_written_as_one_json_line(dbt):
    dbt.log_event("Porch", "camera.porch", "live", {
        "summary": "A courier.", "analysis": "Box in hand.", "category": "delivery",
        "notable": 1, "speak": "A parcel, sir.", "extra": "ignored"})
    lines = open(_path(dbt), encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {"ts", "camera", "entity_id", "image_source", "summary",
                        "analysis", "category", "notable", "speak"}
    assert rec["camera"] == "Porch" and rec["entity_id"] == "camera.porch"
    assert rec["image_source"] == "live" and rec["summary"] == "A courier."
    assert rec["analysis"] == "Box in hand." and rec["category"] == "delivery"
    assert rec["notable"] is True and rec["speak"] == "A parcel, sir."
    assert rec["ts"].endswith("Z")
    stamp = datetime.fromisoformat(rec["ts"][:-1])
    assert abs((datetime.utcnow() - stamp).total_seconds()) < 60


def test_missing_result_fields_default(dbt):
    dbt.log_event("Porch", "camera.porch", "eufy", None)
    dbt.log_event("Porch", "camera.porch", "eufy", {"speak": None})
    a, b = dbt.load_events()
    for rec in (a, b):
        assert rec["summary"] == "" and rec["analysis"] == "" and rec["category"] == ""
        assert rec["notable"] is False and rec["speak"] == ""


def test_non_ascii_text_is_kept_as_is(dbt):
    dbt.log_event("Porch", "camera.porch", "live", {"summary": "Café — délivery"})
    assert "Café — délivery" in open(_path(dbt), encoding="utf-8").read()


def test_the_log_keeps_only_the_newest_records(dbt, monkeypatch):
    monkeypatch.setattr(dbt, "MAX_RECORDS", 3)
    for i in range(5):
        dbt.log_event("Porch", "camera.porch", "live", {"summary": str(i)})
    assert [e["summary"] for e in dbt.load_events()] == ["2", "3", "4"]


def test_a_write_failure_is_swallowed(dbt, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(dbt, "LOG_DIR", str(blocker / "sub"))
    dbt.log_event("Porch", "camera.porch", "live", {})      # no exception


# ── reading ─────────────────────────────────────────────────────────────────

def test_no_log_reads_as_empty(dbt):
    assert dbt.load_events() == [] and dbt.load_events(limit=3) == []


def test_blank_and_broken_lines_are_skipped(dbt):
    _rows(dbt, [{"n": 1}, "", "not json", {"n": 2}, "   "])
    assert dbt.load_events() == [{"n": 1}, {"n": 2}]


def test_a_limit_keeps_the_newest(dbt):
    _rows(dbt, [{"n": i} for i in range(5)])
    assert dbt.load_events(limit=2) == [{"n": 3}, {"n": 4}]
    assert len(dbt.load_events(limit=0)) == 5


def test_stats_count_by_category_and_source(dbt):
    _rows(dbt, [
        {"ts": "a", "category": "delivery", "image_source": "live", "notable": True},
        {"ts": "b", "category": "delivery", "image_source": "backlog"},
        {"ts": "c"},
    ])
    assert dbt.stats() == {
        "total": 3, "notable": 1,
        "by_category": {"delivery": 2, "?": 1},
        "by_source": {"live": 1, "backlog": 1, "?": 1},
        "first": "a", "last": "c",
    }


def test_stats_of_an_empty_log(dbt):
    assert dbt.stats() == {"total": 0, "notable": 0, "by_category": {},
                           "by_source": {}, "first": None, "last": None}


# ── pattern mining ──────────────────────────────────────────────────────────

def test_a_visitor_on_several_days_is_a_pattern(dbt):
    _rows(dbt, [_ev(_ago(d, 10)) for d in (1, 2, 4)] + [_ev(_ago(3, 11))])
    (p,) = dbt.find_patterns()
    assert p["camera"] == "Porch" and p["category"] == "delivery"
    assert p["occurrences"] == 4 and p["distinct_days"] == 4
    assert p["observed_days"] == 4 and p["typical_hour"] == 10
    assert p["consistency"] == 1.0
    assert p["description"] == ("A delivery at Porch, mostly around 10:00 — "
                                "seen on 4 of the last 4 days")


def test_consistency_counts_visits_within_an_hour_either_side(dbt):
    _rows(dbt, [_ev(_ago(d, h)) for d, h in ((1, 23), (2, 23), (3, 0), (4, 5))])
    (p,) = dbt.find_patterns()
    assert p["typical_hour"] == 23 and p["consistency"] == 0.75


def test_camera_and_category_fall_back(dbt):
    rows = [{"ts": _ago(d, 8), "entity_id": "camera.side"} for d in (1, 2, 3)]
    rows += [{"ts": _ago(d, 9), "category": "mail"} for d in (1, 2, 3)]
    _rows(dbt, rows)
    found = {(p["camera"], p["category"]) for p in dbt.find_patterns()}
    assert found == {("camera.side", "other"), ("camera", "mail")}


def test_unknown_categories_use_their_own_name(dbt):
    _rows(dbt, [_ev(_ago(d, 7), category="drone") for d in (1, 2, 3)])
    assert dbt.find_patterns()[0]["description"].startswith("Drone at Porch")


def test_bad_timestamps_and_old_events_are_ignored(dbt):
    _rows(dbt, [_ev("garbage")] + [_ev(_ago(d, 7)) for d in (40, 41, 42)])
    assert dbt.find_patterns() == []
    assert len(dbt.find_patterns(lookback_days=60)) == 1


def test_a_null_timestamp_stops_all_mining(dbt):
    """Preserved quirk: a record whose ts is null makes mining give up."""
    _rows(dbt, [_ev(None)] + [_ev(_ago(d, 7)) for d in (1, 2, 3)])
    assert dbt.find_patterns() == []


def test_a_lower_threshold_and_the_observed_day_cap(dbt):
    _rows(dbt, [_ev(_ago(d, 7)) for d in (0, 20)])
    assert dbt.find_patterns() == []
    (p,) = dbt.find_patterns(min_occurrences=2, lookback_days=10 + 30)
    assert p["observed_days"] == 21


def test_at_most_eight_patterns_most_regular_first(dbt):
    rows = []
    for i in range(10):
        days = range(1, 4 + (i % 3))
        rows += [_ev(_ago(d, 6), camera=f"Cam {i}") for d in days]
    _rows(dbt, rows)
    found = dbt.find_patterns()
    assert len(found) == 8
    assert [p["distinct_days"] for p in found] == sorted(
        (p["distinct_days"] for p in found), reverse=True)


def test_mining_a_broken_log_returns_nothing(dbt, monkeypatch):
    monkeypatch.setattr(dbt, "load_events", lambda limit=None: [{"ts": 5}])
    assert dbt.find_patterns() == []


def test_the_category_labels(dbt):
    assert dbt.CATEGORY_LABEL == {
        "delivery": "a delivery", "package": "a package drop-off", "mail": "mail",
        "person": "someone", "known_resident": "a resident", "vehicle": "a vehicle",
        "animal": "an animal", "empty": "nothing", "other": "something"}


# ── finding Nest doorbells ──────────────────────────────────────────────────

class _Dev:
    def __init__(self, emm=object()):
        self.event_media_manager = emm


def _hass(entries, data=None, loaded=True):
    ce = types.SimpleNamespace()
    if loaded:
        ce.async_loaded_entries = lambda domain: entries if domain == "nest" else []
    else:
        ce.async_entries = lambda domain: entries if domain == "nest" else []
    return types.SimpleNamespace(config_entries=ce, data=data or {})


def test_devices_come_from_the_runtime_device_manager(dbt):
    with_media, without = _Dev(), _Dev(None)
    dm = types.SimpleNamespace(devices={"d1": with_media, "d2": without})
    entry = types.SimpleNamespace(runtime_data=types.SimpleNamespace(device_manager=dm))
    assert dbt._iter_nest_doorbell_devices(_hass([entry])) == [("d1", with_media)]


def test_a_dict_runtime_data_works_too(dbt):
    dev = _Dev()
    entry = types.SimpleNamespace(runtime_data={"device_manager":
                                                types.SimpleNamespace(devices={"d": dev})})
    assert dbt._iter_nest_doorbell_devices(_hass([entry])) == [("d", dev)]


def test_older_cores_use_the_nest_data_and_loaded_entries(dbt):
    dev = _Dev()
    loaded = types.SimpleNamespace(entry_id="e1", state="ConfigEntryState.LOADED",
                                   runtime_data=None)
    failed = types.SimpleNamespace(entry_id="e2", state="setup_error", runtime_data=None)
    data = {"nest": {"e1": {"device_manager": types.SimpleNamespace(devices={"d": dev})},
                     "e2": {"device_manager": types.SimpleNamespace(devices={"x": _Dev()})}}}
    hass = _hass([loaded, failed], data=data, loaded=False)
    assert dbt._iter_nest_doorbell_devices(hass) == [("d", dev)]


def test_no_nest_means_no_devices(dbt):
    assert dbt._iter_nest_doorbell_devices(_hass([])) == []
    broken = types.SimpleNamespace(config_entries=types.SimpleNamespace(), data={})
    assert dbt._iter_nest_doorbell_devices(broken) == []
    entry = types.SimpleNamespace(runtime_data=types.SimpleNamespace(device_manager=None),
                                  entry_id="e")
    assert dbt._iter_nest_doorbell_devices(_hass([entry])) == []


# ── recorded events and their images ───────────────────────────────────────

async def _agen(items):
    for i in items:
        yield i


async def test_clip_and_image_sessions_are_listed(dbt):
    async def images():
        return ["i1"]

    def boom():
        raise RuntimeError("x")
    emm = types.SimpleNamespace(async_clip_preview_sessions=lambda: _agen(["c1", "c2"]),
                                async_image_sessions=images)
    assert await dbt._events_for_device(_Dev(emm)) == [
        ("clip", "c1"), ("clip", "c2"), ("image", "i1")]
    emm2 = types.SimpleNamespace(async_clip_preview_sessions=boom)
    assert await dbt._events_for_device(_Dev(emm2)) == []
    assert await dbt._events_for_device(_Dev(None)) == []


def _media(contents):
    return types.SimpleNamespace(contents=contents)


async def test_an_image_comes_from_the_kind_appropriate_getter_first(dbt):
    calls = []
    jpeg = b"\xff\xd8" + b"x" * 3000

    def getter(name, result):
        async def fn(token):
            calls.append((name, token))
            return result
        return fn
    emm = types.SimpleNamespace(
        get_clip_thumbnail_from_token=getter("thumb", _media(jpeg)),
        get_media_from_token=getter("media", _media(jpeg)))
    ev = types.SimpleNamespace(event_token="tok")
    assert await dbt._event_image_bytes(_Dev(emm), "clip", ev) == jpeg
    assert calls == [("thumb", "tok")]
    calls.clear()
    assert await dbt._event_image_bytes(_Dev(emm), "image", ev) == jpeg
    assert calls == [("media", "tok")]


async def test_video_small_or_missing_media_is_refused(dbt):
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"x" * 3000

    async def video(token):
        return _media(mp4)

    async def tiny(token):
        return _media(b"x" * 100)
    ev = types.SimpleNamespace(event_token="tok")
    emm = types.SimpleNamespace(get_media_from_token=video,
                                get_clip_thumbnail_from_token=tiny)
    assert await dbt._event_image_bytes(_Dev(emm), "image", ev) is None
    assert await dbt._event_image_bytes(_Dev(emm), "image",
                                        types.SimpleNamespace()) is None
    assert await dbt._event_image_bytes(_Dev(None), "image", ev) is None


# ── the backlog scan ────────────────────────────────────────────────────────

async def test_scan_with_no_nest_explains_why(dbt, monkeypatch):
    monkeypatch.setattr(dbt, "_iter_nest_doorbell_devices", lambda hass: [])
    report = await dbt.scan_backlog(None, None, "sir")
    assert report["ok"] is False and report["devices"] == 0 and report["analyzed"] == 0
    assert "No loaded Nest config entry" in report["reason"]


async def test_scan_analyses_and_logs_up_to_the_limit(dbt, monkeypatch):
    evs = [types.SimpleNamespace(event_types=["sdm.devices.events.DoorbellChime.Chime"]),
           types.SimpleNamespace(event_type="camera_person"),
           types.SimpleNamespace(),
           types.SimpleNamespace()]
    monkeypatch.setattr(dbt, "_iter_nest_doorbell_devices", lambda hass: [("dev", _Dev())])

    async def events(dev):
        return [("clip", e) for e in evs]

    async def image(dev, kind, ev):
        return None if ev is evs[2] else b"jpeg"
    labels = []

    async def analyse(img, label):
        labels.append(label)
        return {"success": True, "summary": label} if label != "camera_person" else {
            "success": True, "camera": "Porch", "summary": "p"}
    monkeypatch.setattr(dbt, "_events_for_device", events)
    monkeypatch.setattr(dbt, "_event_image_bytes", image)
    report = await dbt.scan_backlog(None, analyse, "sir", limit=2)
    assert labels == ["Chime", "camera_person"]
    assert report["analyzed"] == 2 and report["ok"] is True and report["found"] == 4
    assert report["no_image"] == 0 and report["reason"] == ""
    logged = dbt.load_events()
    assert [(e["camera"], e["entity_id"], e["image_source"]) for e in logged] == [
        ("dev", "nest:dev", "backlog"), ("Porch", "nest:dev", "backlog")]


async def test_scan_reports_events_without_images(dbt, monkeypatch):
    monkeypatch.setattr(dbt, "_iter_nest_doorbell_devices", lambda hass: [("dev", _Dev())])

    async def events(dev):
        return [("clip", types.SimpleNamespace())] * 2

    async def image(dev, kind, ev):
        return None
    monkeypatch.setattr(dbt, "_events_for_device", events)
    monkeypatch.setattr(dbt, "_event_image_bytes", image)
    report = await dbt.scan_backlog(None, None, "sir")
    assert report["no_image"] == 2 and report["ok"] is False
    assert report["reason"].startswith("Found 2 event(s) but none yielded an image")


async def test_scan_collects_at_most_five_errors(dbt, monkeypatch):
    monkeypatch.setattr(dbt, "_iter_nest_doorbell_devices",
                        lambda hass: [("a", _Dev()), ("b", _Dev())])

    async def events(dev):
        if dev is not None and getattr(dev, "fail", False):
            raise RuntimeError("down")
        return [("image", types.SimpleNamespace(event_type="t"))] * 7

    async def image(dev, kind, ev):
        return b"jpeg"

    async def analyse(img, label):
        raise RuntimeError("model down")
    monkeypatch.setattr(dbt, "_events_for_device", events)
    monkeypatch.setattr(dbt, "_event_image_bytes", image)
    report = await dbt.scan_backlog(None, analyse, "sir")
    assert len(report["errors"]) == 5 and report["errors"][0] == "model down"
    assert report["reason"] == ("Found 14 event record(s) but could not extract "
                                "analysable images (0 without media).")


async def test_scan_records_a_device_whose_events_fail(dbt, monkeypatch):
    monkeypatch.setattr(dbt, "_iter_nest_doorbell_devices", lambda hass: [("a", _Dev())])

    async def events(dev):
        raise RuntimeError("down")
    monkeypatch.setattr(dbt, "_events_for_device", events)
    report = await dbt.scan_backlog(None, None, "sir", limit=0)
    assert report["errors"] == ["events:down"] and report["found"] == 0


def test_stats_with_a_record_missing_its_time_raises(dbt):
    """Preserved quirk: the first or last record without a ts breaks stats."""
    _rows(dbt, [{"category": "delivery"}])
    with pytest.raises(KeyError):
        dbt.stats()
