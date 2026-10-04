"""Notifications given three days running go quiet; emergencies never do."""
import datetime
import json

import pytest


def _ts(day, hour=21):
    return datetime.datetime(2026, 10, day, hour, 0).timestamp()


@pytest.fixture
def hab(load, tmp_path, monkeypatch):
    h = load("habituation")
    monkeypatch.setattr(h, "STATE_FILE", str(tmp_path / "habituation.json"))
    monkeypatch.setattr(h, "_state", None)
    return h


def test_third_day_running_carries_the_note_then_goes_quiet(hab):
    key = "binary_sensor.disk"
    for day in (1, 2):
        assert hab.with_note(key, "Disk busy.", _ts(day)) == "Disk busy."
        hab.record(key, key, _ts(day))
        assert not hab.is_quiet(key)
    assert hab.with_note(key, "Disk busy.", _ts(3)) == "Disk busy. " + hab.QUIET_NOTE
    hab.record(key, key, _ts(3))
    assert hab.is_quiet(key)


def test_same_day_repeats_count_once(hab):
    key = "switch.light"
    for day in (1, 2):
        for hour in (20, 21):
            hab.record(key, key, _ts(day, hour))
    assert not hab.is_quiet(key)


def test_a_missed_day_starts_the_count_again(hab):
    key = "binary_sensor.disk"
    for day in (1, 2, 4, 5):
        hab.record(key, key, _ts(day))
    assert not hab.is_quiet(key)
    assert hab.with_note(key, "x", _ts(6)).endswith(hab.QUIET_NOTE)
    hab.record(key, key, _ts(6))
    assert hab.is_quiet(key)


def test_quiet_survives_a_restart(hab, monkeypatch):
    for day in (1, 2, 3):
        hab.record("k", "sensor.a", _ts(day))
    monkeypatch.setattr(hab, "_state", None)
    assert hab.is_quiet("k")
    with open(hab.STATE_FILE) as f:
        assert json.load(f)["k"]["quiet"] is True


def test_forget_brings_it_back(hab):
    for day in (1, 2, 3):
        hab.record("routine:p:switch.front_light:on", "switch.front_light", _ts(day))
    assert hab.forget("switch.front_*") == ["routine:p:switch.front_light:on"]
    assert not hab.is_quiet("routine:p:switch.front_light:on")
    assert hab.quiet_list() == []


@pytest.mark.parametrize("kw", [
    {"urgency": "critical"},
    {"kind": "intrusion_investigating"},
    {"kind": "lockdown_breach"},
    {"kind": "freeze_warning", "urgency": "high"},
    {"entity_id": "lock.back_door"},
    {"entity_id": "alarm_control_panel.home"},
])
def test_emergencies_are_exempt(hab, kw):
    assert hab.exempt(**kw)


def test_ordinary_alerts_are_not_exempt(hab):
    assert not hab.exempt(urgency="high", kind="problem", entity_id="binary_sensor.disk")
    assert not hab.exempt(urgency="low", kind="anticipation_routine", entity_id="switch.x")


def test_output_gate_goes_quiet_after_three_spoken_days(hab, load, monkeypatch):
    og = load("output_gate")
    monkeypatch.setattr(og, "_now", lambda: _ts(1))
    og._STATE.history.clear()
    og._STATE.recent_messages.clear()
    for day in (1, 2, 3):
        monkeypatch.setattr(hab.time, "time", lambda d=day: _ts(d))
        ok, _ = og.can_announce(entity_id="binary_sensor.disk", category="problem",
                                urgency="high", message="Disk busy")
        assert ok
        msg = og.habit_note(entity_id="binary_sensor.disk", category="problem",
                            urgency="high", message="Disk busy.")
        assert msg.endswith(hab.QUIET_NOTE) == (day == 3)
        og.record_announcement(entity_id="binary_sensor.disk", category="problem",
                               urgency="high", message=msg, was_spoken=True)
        og._STATE.recent_messages.clear()
    ok, reason = og.can_announce(entity_id="binary_sensor.disk", category="problem",
                                 urgency="high", message="Disk busy")
    assert not ok and "normal for this home" in reason
    ok, _ = og.can_announce(entity_id="binary_sensor.disk", category="problem",
                            urgency="critical", message="Disk busy")
    assert ok


def test_unignore_restores_a_quiet_notification(hab, load):
    core = load("cognitive_core")
    for day in (1, 2, 3):
        hab.record("binary_sensor.disk", "binary_sensor.disk", _ts(day))
    core._CORE.ignore_mgr = None
    assert [r["pattern"] for r in core.list_ignores()] == ["binary_sensor.disk"]
    out = core.unignore("binary_sensor.disk")
    assert out["success"] is True and out["restored_notifications"] == 1
    assert not hab.is_quiet("binary_sensor.disk")


async def test_save_on_the_event_loop_runs_in_the_executor(hab, monkeypatch):
    """record() is called from async alert code: the file write must not run
    on the loop thread."""
    import asyncio
    import threading
    loop_thread = threading.get_ident()
    writers = []
    real_write = hab._write

    def spy(snapshot, gen):
        real_write(snapshot, gen)
        writers.append(threading.get_ident())

    monkeypatch.setattr(hab, "_write", spy)
    hab.record("k", "k", _ts(1))
    for _ in range(50):
        if writers:
            break
        await asyncio.sleep(0.01)
    assert writers and loop_thread not in writers
    with open(hab.STATE_FILE) as f:
        assert json.load(f)["k"]["streak"] == 1


def test_an_older_snapshot_never_overwrites_a_newer_one(hab, monkeypatch):
    monkeypatch.setattr(hab, "_written_gen", 0)
    hab._write({"k": {"streak": 2}}, 2)
    hab._write({"k": {"streak": 1}}, 1)
    with open(hab.STATE_FILE) as f:
        assert json.load(f)["k"]["streak"] == 2
