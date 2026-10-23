"""The intrusion event log, its real/false labels and learned damping.

What matters most for safety: labels can only quieten the weak alerts, and a
single "real" label switches damping off for that place and time of day, so
Nova can never learn to ignore somewhere a genuine break-in happened.

Every test runs on a pinned clock, so a run that straddles one of the
three hour pattern slots can never flip a result.
"""
import json
import time as real_time
import types

import pytest

# Noon local time on a fixed day: well inside the 12:00-15:00 slot.
NOON = real_time.mktime((2026, 3, 4, 12, 0, 0, 0, 0, -1))


class Clock:
    def __init__(self, start):
        self.now = start

    def time(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(NOON)


@pytest.fixture
def ilog(load, tmp_path, monkeypatch, clock):
    mod = load("intrusion")
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", str(tmp_path / "shots"))
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "events.json")
    # Only this module's view of `time` moves; localtime still works on it.
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(
        time=clock.time, localtime=real_time.localtime))
    monkeypatch.setattr(mod, "_log", [])
    monkeypatch.setattr(mod, "_log_loaded", True)   # start from an empty log
    monkeypatch.setattr(mod, "_called_off_until", 0.0)
    monkeypatch.setattr(mod, "_false_alarms", [])
    yield mod
    # Hand later tests an empty, already loaded log rather than whatever other
    # files left behind: leftover "false" labels would quietly damp their
    # first alerts.
    monkeypatch.undo()
    mod._log = []
    mod._log_loaded = True


def mark_false(mod, how_many, area="kitchen"):
    """Log `how_many` first alerts in `area`, each labelled a false alarm."""
    for _ in range(how_many):
        event = mod.record_event("investigating", breach_area=area)
        assert mod.label_event(event["id"], "false")["ok"] is True


# ── recording ───────────────────────────────────────────────────────────────

def test_a_recorded_event_holds_what_it_was_given(ilog):
    event = ilog.record_event("investigating", reason="motion",
                              breach="kitchen window", breach_area="kitchen")
    assert (event["kind"], event["reason"]) == ("investigating", "motion")
    assert (event["breach"], event["breach_area"]) == ("kitchen window", "kitchen")
    assert event["label"] is None
    assert event["ts"] == NOON
    assert event["pattern"] == "kitchen|4"
    assert ilog.get_log() == [event]


def test_each_event_gets_its_own_id(ilog):
    ids = {ilog.record_event("investigating")["id"] for _ in range(5)}
    assert len(ids) == 5


def test_the_log_reads_newest_first(ilog):
    for word in ("one", "two", "three"):
        ilog.record_event("investigating", reason=word)
    assert [ev["reason"] for ev in ilog.get_log()] == ["three", "two", "one"]


def test_the_log_is_written_to_disk_and_reads_back(ilog, monkeypatch):
    ilog.record_event("confirmed", reason="real one")
    assert ilog.LOG_PATH.exists()
    on_disk = json.loads(ilog.LOG_PATH.read_text())
    assert on_disk[0]["reason"] == "real one"
    # A restart forgets memory; the file brings the event back.
    monkeypatch.setattr(ilog, "_log", [])
    monkeypatch.setattr(ilog, "_log_loaded", False)
    assert [ev["reason"] for ev in ilog.get_log()] == ["real one"]


def test_the_log_keeps_only_the_newest_events(ilog):
    extra = 25
    for n in range(ilog._MAX_LOG + extra):
        ilog.record_event("investigating", reason=str(n))
    assert len(ilog._log) == ilog._MAX_LOG
    assert ilog._log[0]["reason"] == str(extra)
    assert ilog._log[-1]["reason"] == str(ilog._MAX_LOG + extra - 1)
    assert len(json.loads(ilog.LOG_PATH.read_text())) == ilog._MAX_LOG


def test_a_snapshot_is_kept_by_path_never_by_public_url(ilog):
    shot = {"path": "/config/nova/intrusion/x.jpg", "camera": "camera.kitchen"}
    event = ilog.record_event("confirmed", snapshot=shot)
    assert event["snapshot_path"] == "/config/nova/intrusion/x.jpg"
    assert event["camera"] == "camera.kitchen"
    # No unauthenticated link may ever be stored with an event.
    assert "snapshot_url" not in event
    assert not any("url" in key for key in event)


def test_a_named_camera_wins_over_the_snapshot_camera(ilog):
    event = ilog.record_event("confirmed", camera="camera.porch",
                              snapshot={"path": "p.jpg", "camera": "camera.kitchen"})
    assert event["camera"] == "camera.porch"


# ── labels ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["real", "false"])
def test_a_label_is_stored_on_the_event(ilog, label):
    event = ilog.record_event("investigating", breach_area="kitchen")
    result = ilog.label_event(event["id"], label)
    assert result == {"ok": True, "id": event["id"], "label": label}
    stored = ilog.get_log()[0]
    assert stored["label"] == label
    assert stored["label_source"] == ilog.LABEL_SOURCE_PANEL


@pytest.mark.parametrize("bad", ["maybe", "REAL", "true", "False"])
def test_an_unknown_label_is_refused_and_changes_nothing(ilog, bad):
    event = ilog.record_event("investigating")
    assert ilog.label_event(event["id"], bad)["ok"] is False
    assert ilog.get_log()[0]["label"] is None


def test_labelling_a_missing_event_is_refused(ilog):
    assert ilog.label_event("nope", "false") == {"ok": False, "error": "event not found"}


@pytest.mark.parametrize("blank", ["", None])
def test_a_label_can_be_taken_back(ilog, blank):
    event = ilog.record_event("investigating")
    ilog.label_event(event["id"], "false")
    assert ilog.label_event(event["id"], blank)["ok"] is True
    stored = ilog.get_log()[0]
    assert stored["label"] is None
    assert stored["label_source"] is None


# ── learned damping of the weak alerts ──────────────────────────────────────

@pytest.mark.parametrize("below", [0, 1, 2])
def test_too_few_false_labels_never_damp(ilog, below):
    assert below < ilog._LEARN_MIN_FALSE
    mark_false(ilog, below)
    assert ilog.should_damp_weak_alert("kitchen", None) is False


def test_damping_starts_exactly_at_the_threshold(ilog):
    mark_false(ilog, ilog._LEARN_MIN_FALSE - 1)
    assert ilog.should_damp_weak_alert("kitchen", None) is False
    mark_false(ilog, 1)
    assert ilog.should_damp_weak_alert("kitchen", None) is True


def test_one_real_label_ends_damping_however_many_false_ones(ilog):
    mark_false(ilog, ilog._LEARN_MIN_FALSE + 3)
    assert ilog.should_damp_weak_alert("kitchen", None) is True
    genuine = ilog.record_event("confirmed", breach_area="kitchen")
    ilog.label_event(genuine["id"], "real")
    assert ilog.should_damp_weak_alert("kitchen", None) is False, \
        "a real intrusion must cancel learned damping for that place"
    # More false alarms afterwards still cannot bring damping back.
    mark_false(ilog, ilog._LEARN_MIN_FALSE * 2)
    assert ilog.should_damp_weak_alert("kitchen", None) is False


def test_a_real_label_alone_never_damps(ilog):
    event = ilog.record_event("confirmed", breach_area="kitchen")
    ilog.label_event(event["id"], "real")
    assert ilog.pattern_verdict("kitchen", None)["damp"] is False


def test_damping_belongs_to_one_place(ilog):
    mark_false(ilog, ilog._LEARN_MIN_FALSE + 1, area="kitchen")
    assert ilog.should_damp_weak_alert("kitchen", None) is True
    assert ilog.should_damp_weak_alert("basement", None) is False


def test_damping_belongs_to_one_time_of_day(ilog, clock):
    mark_false(ilog, ilog._LEARN_MIN_FALSE + 1)
    clock.now = NOON + 3 * 3600          # the next three hour slot
    assert ilog.should_damp_weak_alert("kitchen", None) is False


def test_labels_still_count_just_inside_the_window(ilog, clock):
    mark_false(ilog, ilog._LEARN_MIN_FALSE)
    # Same slot of a later day, one day short of the window's end.
    clock.now = NOON + ilog._LEARN_WINDOW - 86400
    assert ilog.should_damp_weak_alert("kitchen", None) is True


def test_labels_older_than_the_window_stop_counting(ilog, clock):
    mark_false(ilog, ilog._LEARN_MIN_FALSE + 1)
    assert ilog.should_damp_weak_alert("kitchen", None) is True
    clock.now = NOON + ilog._LEARN_WINDOW + 100
    assert ilog.should_damp_weak_alert("kitchen", None) is False


def test_events_nobody_labelled_never_damp(ilog):
    for _ in range(10):
        ilog.record_event("investigating", breach_area="kitchen")
    verdict = ilog.pattern_verdict("kitchen", None)
    assert (verdict["false_count"], verdict["real_count"], verdict["damp"]) == (0, 0, False)
    assert ilog.should_damp_weak_alert("kitchen", None) is False


def test_the_learning_summary(ilog):
    mark_false(ilog, ilog._LEARN_MIN_FALSE)
    ilog.record_event("investigating", breach_area="hall")   # left unlabelled
    summary = ilog.learning_summary()
    assert summary["events"] == ilog._LEARN_MIN_FALSE + 1
    assert summary["labeled"] == ilog._LEARN_MIN_FALSE
    assert summary["damped_patterns"] == ["kitchen|4"]
    assert summary["patterns"]["kitchen|4"] == {"false": ilog._LEARN_MIN_FALSE, "real": 0}
    assert summary["min_false_to_damp"] == ilog._LEARN_MIN_FALSE


def test_no_place_at_all_is_a_plain_no(ilog):
    assert ilog.should_damp_weak_alert(None, None) is False


def test_a_fault_while_judging_means_no_damping(ilog, monkeypatch):
    mark_false(ilog, ilog._LEARN_MIN_FALSE + 1)

    def broken(*_args, **_kwargs):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(ilog, "pattern_verdict", broken)
    assert ilog.should_damp_weak_alert("kitchen", None) is False
