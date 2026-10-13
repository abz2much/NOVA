"""Acceptance tests for the periodic-task scheduler, written from its
behaviour spec (specs/scheduler.md). No real Home Assistant: every timer is
created through an injected recording tracker."""
from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
import types
from datetime import timedelta

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

class Recorder:
    """Stand-in tracker: remembers each call and returns a counting unsub."""

    def __init__(self, unsub_factory=None):
        self.calls = []          # (hass, wrapper, interval)
        self.unsub_calls = []    # registration index per unsub call
        self._factory = unsub_factory

    def __call__(self, hass, wrapper, interval):
        idx = len(self.calls)
        self.calls.append((hass, wrapper, interval))
        if self._factory is not None:
            return self._factory(idx)

        def _unsub():
            self.unsub_calls.append(idx)
        return _unsub

    def wrapper(self, idx=0):
        return self.calls[idx][1]


@pytest.fixture
def mod(load):
    return load("scheduler")


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def sched(mod, rec):
    return mod.NovaScheduler(hass=None, tracker=rec)


async def _noop(now=None):
    return None


def _entry(s, name):
    return next(e for e in s.status() if e["name"] == name)


STATUS_KEYS = {"name", "interval_s", "runs", "errors", "consecutive_errors",
               "last_run", "last_success", "last_duration_ms", "last_error",
               "healthy"}


# ── construction & private names ──────────────────────────────────────────────

def test_fresh_scheduler_is_empty(sched):
    assert sched.task_names() == []
    assert sched.status() == []
    assert sched.shutdown() == 0


def test_hass_is_kept_by_identity_and_accepted_positionally(mod, rec):
    marker = object()
    s = mod.NovaScheduler(marker, tracker=rec)
    assert s._hass is marker
    assert mod.NovaScheduler()._hass is None


def test_tasks_dict_holds_raw_callback(sched):
    async def job(now=None):
        return None
    sched.add("alpha", 30, job)
    assert isinstance(sched._tasks, dict)
    assert list(sched._tasks) == ["alpha"]
    assert sched._tasks["alpha"].cb is job


async def test_awaiting_raw_callback_records_nothing(sched):
    seen = []

    async def job(now=None):
        seen.append(now)
    sched.add("alpha", 30, job)
    await sched._tasks["alpha"].cb(None)
    assert seen == [None]
    e = _entry(sched, "alpha")
    assert e["runs"] == 0 and e["last_run"] is None


# ── add ───────────────────────────────────────────────────────────────────────

def test_add_calls_tracker_once_with_hass_wrapper_and_timedelta(mod, rec):
    marker = object()
    s = mod.NovaScheduler(hass=marker, tracker=rec)
    assert s.add("alpha", 45, _noop) is True
    assert len(rec.calls) == 1
    hass, wrapper, interval = rec.calls[0]
    assert hass is marker
    assert interval == timedelta(seconds=45)
    assert isinstance(interval, timedelta)
    assert inspect.iscoroutinefunction(wrapper)
    assert wrapper is not _noop


def test_tracker_receives_none_hass(rec, sched):
    sched.add("alpha", 1, _noop)
    assert rec.calls[0][0] is None


@pytest.mark.parametrize("interval, secs", [
    (10, 10.0), (2.5, 2.5), ("7", 7.0), (timedelta(minutes=2), 120.0),
    (timedelta(milliseconds=1500), 1.5),
])
def test_interval_forms(sched, rec, interval, secs):
    assert sched.add("alpha", interval, _noop) is True
    assert rec.calls[0][2].total_seconds() == pytest.approx(secs)


def test_names_kept_in_registration_order(sched):
    for n in ("charlie", "alpha", "bravo"):
        sched.add(n, 5, _noop)
    assert sched.task_names() == ["charlie", "alpha", "bravo"]
    assert [e["name"] for e in sched.status()] == ["charlie", "alpha", "bravo"]


def test_task_names_returns_a_new_list(sched):
    sched.add("alpha", 5, _noop)
    names = sched.task_names()
    names.append("bogus")
    assert sched.task_names() == ["alpha"]


def test_duplicate_name_skipped_first_wins(sched, rec, caplog):
    async def other(now=None):
        return None
    assert sched.add("alpha", 10, _noop) is True
    with caplog.at_level(logging.DEBUG):
        assert sched.add("alpha", 99, other) is False
    assert len(rec.calls) == 1
    assert sched._tasks["alpha"].cb is _noop
    assert _entry(sched, "alpha")["interval_s"] == 10
    assert sched.task_names() == ["alpha"]


def test_duplicate_with_bad_interval_returns_false_without_raising(sched, rec):
    sched.add("alpha", 10, _noop)
    assert sched.add("alpha", "not-a-number", _noop) is False
    assert len(rec.calls) == 1


@pytest.mark.parametrize("bad", ["not-a-number", None])
def test_bad_interval_for_new_name_raises(sched, rec, bad):
    with pytest.raises((ValueError, TypeError)):
        sched.add("alpha", bad, _noop)
    assert rec.calls == []
    assert sched.task_names() == []


def test_tracker_exception_returns_false_and_logs(mod, caplog):
    def broken(hass, cb, interval):
        raise RuntimeError("tracker down")
    s = mod.NovaScheduler(tracker=broken)
    with caplog.at_level(logging.WARNING):
        assert s.add("alpha", 5, _noop) is False
    assert s.task_names() == []
    assert "alpha" not in s._tasks
    assert any(r.levelno == logging.WARNING and "alpha" in r.getMessage()
               for r in caplog.records)


def test_name_is_free_after_tracker_failure(mod):
    state = {"fail": True}
    calls = []

    def flaky(hass, cb, interval):
        calls.append(1)
        if state["fail"]:
            raise OSError("nope")
        return lambda: None
    s = mod.NovaScheduler(tracker=flaky)
    assert s.add("alpha", 5, _noop) is False
    state["fail"] = False
    assert s.add("alpha", 5, _noop) is True
    assert len(calls) == 2


def test_falsy_unsub_still_registers(mod):
    s = mod.NovaScheduler(tracker=lambda h, c, i: None)
    assert s.add("alpha", 5, _noop) is True
    assert s.task_names() == ["alpha"]
    assert s.shutdown() == 0
    assert s.task_names() == []


# ── default tracker (lazy Home Assistant import) ──────────────────────────────

def test_default_tracker_uses_lazy_ha_import(mod, monkeypatch):
    got = []

    def fake_track(hass, cb, interval):
        got.append((hass, cb, interval))
        return lambda: got.append("unsub")
    fake = types.ModuleType("homeassistant.helpers.event")
    fake.async_track_time_interval = fake_track
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", fake)

    marker = object()
    s = mod.NovaScheduler(marker)
    assert s.add("alpha", 12, _noop) is True
    assert len(got) == 1
    assert got[0][0] is marker
    assert got[0][2] == timedelta(seconds=12)
    assert inspect.iscoroutinefunction(got[0][1])
    assert s.shutdown() == 1
    assert got[-1] == "unsub"


def test_default_tracker_import_failure_is_contained(mod, monkeypatch):
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", None)
    s = mod.NovaScheduler(object())
    assert s.add("alpha", 12, _noop) is False
    assert s.task_names() == []


def test_module_imports_without_home_assistant(mod):
    src = inspect.getsource(mod)
    top = [l for l in src.splitlines()
           if l.startswith(("import ", "from ")) and "homeassistant" in l]
    assert top == []


# ── running ticks ─────────────────────────────────────────────────────────────

async def test_wrapper_passes_now_through(sched, rec):
    seen = []

    async def job(now=None):
        seen.append(now)
    sched.add("alpha", 5, job)
    stamp = object()
    await rec.wrapper()(stamp)
    await rec.wrapper()()
    assert seen == [stamp, None]


async def test_wrapper_returns_none(sched, rec):
    async def job(now=None):
        return "value"
    sched.add("alpha", 5, job)
    assert await rec.wrapper()(None) is None


async def test_successful_tick_bookkeeping(sched, rec):
    sched.add("alpha", 5, _noop)
    before = time.time()
    await rec.wrapper()(None)
    after = time.time()
    e = _entry(sched, "alpha")
    assert e["runs"] == 1 and e["errors"] == 0 and e["consecutive_errors"] == 0
    assert before <= e["last_run"] <= after
    assert e["last_run"] <= e["last_success"] <= after
    assert isinstance(e["last_run"], float) and isinstance(e["last_success"], float)
    assert e["last_error"] == "" and e["healthy"] is True


async def test_failed_tick_is_contained_and_recorded(sched, rec, caplog):
    async def job(now=None):
        raise KeyError("missing thing")
    sched.add("alpha", 5, job)
    with caplog.at_level(logging.WARNING):
        await rec.wrapper()(None)          # must not raise
    e = _entry(sched, "alpha")
    assert e["runs"] == 1 and e["errors"] == 1 and e["consecutive_errors"] == 1
    assert e["last_error"] == f"KeyError: {KeyError('missing thing')}"
    assert e["last_run"] is not None and e["last_success"] is None
    assert e["healthy"] is False
    assert any(r.levelno == logging.WARNING and "alpha" in r.getMessage()
               for r in caplog.records)


async def test_error_counters_across_failures_and_recovery(sched, rec):
    mode = {"fail": True}

    async def job(now=None):
        if mode["fail"]:
            raise ValueError("bad tick")
    sched.add("alpha", 5, job)
    w = rec.wrapper()
    await w(None)
    await w(None)
    await w(None)
    e = _entry(sched, "alpha")
    assert (e["runs"], e["errors"], e["consecutive_errors"]) == (3, 3, 3)
    assert e["last_error"] == "ValueError: bad tick"

    mode["fail"] = False
    await w(None)
    e = _entry(sched, "alpha")
    assert (e["runs"], e["errors"], e["consecutive_errors"]) == (4, 3, 0)
    assert e["last_error"] == "" and e["healthy"] is True


async def test_last_success_survives_a_later_failure(sched, rec):
    mode = {"fail": False}

    async def job(now=None):
        if mode["fail"]:
            raise RuntimeError("later")
    sched.add("alpha", 5, job)
    await rec.wrapper()(None)
    first_ok = _entry(sched, "alpha")["last_success"]
    mode["fail"] = True
    await rec.wrapper()(None)
    e = _entry(sched, "alpha")
    assert e["last_success"] == first_ok
    assert e["last_run"] >= first_ok


async def test_duration_measured_in_whole_ms(sched, rec):
    async def slow(now=None):
        await asyncio.sleep(0.03)
    sched.add("alpha", 5, slow)
    await rec.wrapper()(None)
    d = _entry(sched, "alpha")["last_duration_ms"]
    assert isinstance(d, int) and 20 <= d < 2000


async def test_duration_recorded_on_failure(sched, rec):
    async def slow_fail(now=None):
        await asyncio.sleep(0.03)
        raise RuntimeError("late")
    sched.add("alpha", 5, slow_fail)
    await rec.wrapper()(None)
    assert _entry(sched, "alpha")["last_duration_ms"] >= 20


async def test_fast_tick_duration_truncates_to_zero(sched, rec):
    sched.add("alpha", 5, _noop)
    await rec.wrapper()(None)
    assert _entry(sched, "alpha")["last_duration_ms"] == 0


async def test_base_exception_propagates_without_error_count(sched, rec):
    async def cancelled(now=None):
        await asyncio.sleep(0.02)
        raise asyncio.CancelledError()
    sched.add("alpha", 5, cancelled)
    with pytest.raises(asyncio.CancelledError):
        await rec.wrapper()(None)
    e = _entry(sched, "alpha")
    assert e["runs"] == 1 and e["last_run"] is not None
    assert e["errors"] == 0 and e["consecutive_errors"] == 0
    assert e["last_duration_ms"] >= 10


async def test_sync_callback_counts_as_failed_tick(sched, rec):
    def not_async(now=None):
        return None
    assert sched.add("alpha", 5, not_async) is True
    await rec.wrapper()(None)
    e = _entry(sched, "alpha")
    assert e["errors"] == 1
    assert e["last_error"].startswith("TypeError: ")


async def test_tasks_are_independent(sched, rec):
    async def bad(now=None):
        raise RuntimeError("x")
    sched.add("good", 5, _noop)
    sched.add("bad", 5, bad)
    await rec.wrapper(1)(None)
    await rec.wrapper(0)(None)
    g, b = _entry(sched, "good"), _entry(sched, "bad")
    assert g["runs"] == 1 and g["errors"] == 0 and g["healthy"] is True
    assert b["runs"] == 1 and b["errors"] == 1 and b["healthy"] is False


# ── status ────────────────────────────────────────────────────────────────────

def test_status_of_never_run_task(sched):
    sched.add("alpha", 60, _noop)
    [e] = sched.status()
    assert set(e) == STATUS_KEYS
    assert e == {"name": "alpha", "interval_s": 60, "runs": 0, "errors": 0,
                 "consecutive_errors": 0, "last_run": None,
                 "last_success": None, "last_duration_ms": 0,
                 "last_error": "", "healthy": True}


@pytest.mark.parametrize("interval, shown", [
    (0.5, 0), (90.9, 90), (timedelta(seconds=1.99), 1), (3600, 3600),
])
def test_status_interval_is_int_truncated(sched, rec, interval, shown):
    sched.add("alpha", interval, _noop)
    v = _entry(sched, "alpha")["interval_s"]
    assert v == shown and type(v) is int
    # the real timer keeps the exact seconds
    exact = interval.total_seconds() if isinstance(interval, timedelta) else interval
    assert rec.calls[0][2].total_seconds() == pytest.approx(exact)


async def test_status_returns_fresh_dicts(sched, rec):
    sched.add("alpha", 5, _noop)
    first = sched.status()
    first[0]["runs"] = 999
    first.clear()
    assert _entry(sched, "alpha")["runs"] == 0
    await rec.wrapper()(None)
    assert _entry(sched, "alpha")["runs"] == 1


def test_status_value_types(sched):
    sched.add("alpha", 5, _noop)
    e = sched.status()[0]
    for key in ("interval_s", "runs", "errors", "consecutive_errors",
                "last_duration_ms"):
        assert type(e[key]) is int, key
    assert type(e["healthy"]) is bool
    assert isinstance(e["last_error"], str) and isinstance(e["name"], str)


# ── shutdown ──────────────────────────────────────────────────────────────────

def test_shutdown_unsubs_each_once_and_clears(sched, rec):
    for n in ("alpha", "bravo", "charlie"):
        sched.add(n, 5, _noop)
    assert sched.shutdown() == 3
    assert sorted(rec.unsub_calls) == [0, 1, 2]
    assert sched.task_names() == [] and sched._tasks == {}
    assert sched.status() == []


def test_shutdown_is_idempotent(sched, rec):
    sched.add("alpha", 5, _noop)
    assert sched.shutdown() == 1
    assert sched.shutdown() == 0
    assert sched.shutdown() == 0
    assert rec.unsub_calls == [0]


def test_add_after_shutdown_registers_again(sched, rec):
    sched.add("alpha", 5, _noop)
    sched.shutdown()
    assert sched.add("alpha", 5, _noop) is True
    assert len(rec.calls) == 2
    assert sched.task_names() == ["alpha"]
    assert sched.shutdown() == 1


def test_raising_unsub_is_swallowed_and_not_counted(mod):
    called = []

    def factory(idx):
        def _unsub():
            called.append(idx)
            if idx == 1:
                raise RuntimeError("unsub broke")
        return _unsub
    s = mod.NovaScheduler(tracker=Recorder(factory))
    for n in ("alpha", "bravo", "charlie"):
        s.add(n, 5, _noop)
    assert s.shutdown() == 2               # the raising one is not counted
    assert sorted(called) == [0, 1, 2]     # but every unsub was attempted
    assert s.task_names() == []
    assert s.shutdown() == 0


def test_mixed_falsy_and_real_unsubs(mod):
    def factory(idx):
        return None if idx % 2 else (lambda: None)
    s = mod.NovaScheduler(tracker=Recorder(factory))
    for n in ("a", "b", "c", "d"):
        s.add(n, 5, _noop)
    assert s.shutdown() == 2
