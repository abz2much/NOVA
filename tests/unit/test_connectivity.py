"""Acceptance tests for connectivity.py, written from its behaviour spec.

Each test gets its own freshly executed copy of the module (so lifetime
totals start at zero) with a controllable clock installed in place of the
module's `time` reference. A few tests use the shared copy from the `load`
fixture to check the names other code relies on.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading

import pytest

COOLDOWN = 60.0


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


_counter = {"n": 0}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cb(load, clock, monkeypatch):
    """A fresh copy of the module whose clock is `clock`."""
    path = load("connectivity").__file__
    _counter["n"] += 1
    spec = importlib.util.spec_from_file_location(
        f"spec_connectivity_copy_{_counter['n']}", path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "time", clock)
    return mod


def _open(cb):
    for _ in range(2):
        cb.record_failure()
    assert cb.status()["state"] == "open"


def _half_open(cb, clock):
    _open(cb)
    clock.advance(COOLDOWN)
    assert cb.allow_request() is True
    assert cb.status()["state"] == "half_open"


# ── public interface and names callers rely on ───────────────────────────────

def test_constants_have_the_documented_values(cb):
    assert cb._FAILURE_THRESHOLD == 2 and isinstance(cb._FAILURE_THRESHOLD, int)
    assert cb._OPEN_COOLDOWN == 60.0 and isinstance(cb._OPEN_COOLDOWN, float)
    assert cb._HALF_OPEN_MAX_PROBES == 1
    assert (cb._CLOSED, cb._OPEN, cb._HALF_OPEN) == ("closed", "open", "half_open")


def test_public_functions_are_plain_sync_callables(cb):
    import inspect
    for name in ("allow_request", "record_success", "record_failure",
                 "is_online", "status", "reset"):
        fn = getattr(cb, name)
        assert callable(fn) and not inspect.iscoroutinefunction(fn)
    assert cb.record_success() is None
    assert cb.record_failure() is None
    assert cb.reset() is None


def test_there_is_no_snapshot_function(cb, load):
    assert not hasattr(cb, "snapshot")
    assert not hasattr(load("connectivity"), "snapshot")


def test_private_state_object_and_logger_exist(cb):
    b = cb._BREAKER
    for attr in ("state", "consecutive_failures", "opened_at", "half_open_probes",
                 "last_success", "last_failure", "total_failures",
                 "total_successes", "lock"):
        assert hasattr(b, attr), attr
    assert hasattr(b.lock, "acquire") and hasattr(b.lock, "release")
    assert cb._LOGGER.name == cb.__name__


def test_state_object_is_mutated_in_place_never_replaced(cb, clock):
    b = cb._BREAKER
    _half_open(cb, clock)
    cb.record_failure()
    cb.record_success()
    cb.reset()
    cb.status()
    assert cb._BREAKER is b


def test_shared_module_from_load_fixture_supports_caller_usage(load, monkeypatch):
    conn = load("connectivity")
    conn.reset()
    try:
        conn.record_failure()
        conn.record_failure()
        assert conn.allow_request() is False
        # rewind the opening time instead of waiting out the cooldown
        conn._BREAKER.opened_at -= conn._OPEN_COOLDOWN + 1
        assert conn.status()["state"] == "open"
        assert conn._BREAKER.half_open_probes == 0
        assert conn.allow_request() is True
        assert conn.allow_request() is False
        conn.record_success()
        assert conn.status()["state"] == "closed"
        monkeypatch.setattr(conn, "allow_request", lambda: True)
        monkeypatch.setattr(conn, "is_online", lambda: False)
        assert conn.allow_request() is True and conn.is_online() is False
    finally:
        monkeypatch.undo()
        conn.reset()


def test_clock_is_read_through_the_module_time_reference(cb, clock):
    clock.now = 500.0
    cb.record_failure()
    assert cb._BREAKER.last_failure == 500.0


def test_patched_constants_take_effect_on_the_next_call(cb, clock, monkeypatch):
    monkeypatch.setattr(cb, "_FAILURE_THRESHOLD", 1)
    cb.record_failure()
    assert cb.status()["state"] == "open"
    monkeypatch.setattr(cb, "_OPEN_COOLDOWN", 5.0)
    clock.advance(5)
    assert cb.allow_request() is True


# ── rule 1: initial state ────────────────────────────────────────────────────

def test_fresh_module_is_closed_and_empty(cb):
    b = cb._BREAKER
    assert b.state == "closed"
    assert (b.consecutive_failures, b.half_open_probes) == (0, 0)
    assert (b.opened_at, b.last_success, b.last_failure) == (0.0, 0.0, 0.0)
    assert (b.total_failures, b.total_successes) == (0, 0)
    assert cb.is_online() is True
    assert cb.status() == {
        "state": "closed", "online": True, "consecutive_failures": 0,
        "cooldown_remaining_s": 0, "total_failures": 0, "total_successes": 0,
        "last_success_ago_s": None, "last_failure_ago_s": None,
    }


# ── rule 2: closed ───────────────────────────────────────────────────────────

def test_closed_always_allows_without_side_effects(cb):
    before = cb.status()
    for _ in range(5):
        assert cb.allow_request() is True
    assert cb.status() == before
    assert cb._BREAKER.half_open_probes == 0


# ── rule 3: record_failure ───────────────────────────────────────────────────

def test_one_failure_stays_closed(cb, clock):
    cb.record_failure()
    st = cb.status()
    assert st["state"] == "closed" and st["online"] is True
    assert st["consecutive_failures"] == 1 and st["total_failures"] == 1
    assert cb._BREAKER.last_failure == clock.now
    assert cb.allow_request() is True


def test_threshold_failures_open_the_breaker(cb, clock):
    cb.record_failure()
    clock.advance(3)
    cb.record_failure()
    assert cb.status()["state"] == "open"
    assert cb._BREAKER.opened_at == clock.now
    assert cb.is_online() is False
    assert cb.allow_request() is False


def test_failure_while_open_restarts_the_cooldown(cb, clock):
    _open(cb)
    clock.advance(50)
    cb.record_failure()
    assert cb._BREAKER.opened_at == clock.now
    assert cb.status()["consecutive_failures"] == 3
    clock.advance(50)            # 100 s after first opening, 50 s after refresh
    assert cb.allow_request() is False
    clock.advance(10)
    assert cb.allow_request() is True


def test_failed_probe_reopens_and_clears_probes(cb, clock):
    _half_open(cb, clock)
    clock.advance(1)
    cb.record_failure()
    assert cb.status()["state"] == "open"
    assert cb._BREAKER.opened_at == clock.now
    assert cb._BREAKER.half_open_probes == 0
    assert cb.allow_request() is False


def test_failed_probe_reopens_even_below_threshold(cb, clock, monkeypatch):
    _half_open(cb, clock)
    monkeypatch.setattr(cb, "_FAILURE_THRESHOLD", 100)
    cb.record_failure()
    assert cb.status()["state"] == "open"


# ── rules 4-6: open and half-open ────────────────────────────────────────────

def test_open_denies_until_the_cooldown_boundary(cb, clock):
    _open(cb)
    clock.advance(COOLDOWN - 0.001)
    assert cb.allow_request() is False
    assert cb.status()["state"] == "open"
    clock.advance(0.001)          # exactly the cooldown counts as elapsed
    assert cb.allow_request() is True
    assert cb.status()["state"] == "half_open"
    assert cb._BREAKER.half_open_probes == 1


def test_half_open_hands_out_one_probe_only(cb, clock):
    _half_open(cb, clock)
    for _ in range(4):
        assert cb.allow_request() is False
    assert cb._BREAKER.half_open_probes == 1


def test_half_open_has_no_timeout(cb, clock):
    _half_open(cb, clock)
    clock.advance(10 * COOLDOWN)
    assert cb.allow_request() is False
    assert cb.status()["state"] == "half_open"


def test_more_probes_when_the_limit_is_raised(cb, clock, monkeypatch):
    monkeypatch.setattr(cb, "_HALF_OPEN_MAX_PROBES", 2)
    _half_open(cb, clock)
    assert cb.allow_request() is True
    assert cb.allow_request() is False


def test_half_open_reports_offline(cb, clock):
    _half_open(cb, clock)
    st = cb.status()
    assert st["online"] is False and cb.is_online() is False
    assert st["cooldown_remaining_s"] == 0


# ── rule 7: record_success ───────────────────────────────────────────────────

def test_successful_probe_closes(cb, clock):
    _half_open(cb, clock)
    opened = cb._BREAKER.opened_at
    cb.record_success()
    b = cb._BREAKER
    assert b.state == "closed" and b.consecutive_failures == 0
    assert b.half_open_probes == 0 and b.last_success == clock.now
    assert b.opened_at == opened
    assert cb.status()["total_successes"] == 1
    assert cb.allow_request() is True and cb.is_online() is True


def test_success_closes_an_open_breaker_without_a_probe(cb):
    _open(cb)
    cb.record_success()
    assert cb.status()["state"] == "closed"


def test_success_while_closed_still_counts(cb, clock):
    cb.record_failure()
    cb.record_success()
    clock.advance(2)
    cb.record_success()
    st = cb.status()
    assert st["total_successes"] == 2 and st["consecutive_failures"] == 0
    assert cb._BREAKER.last_success == clock.now


def test_success_breaks_the_failure_streak(cb):
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.status()["state"] == "closed"
    assert cb.status()["total_failures"] == 2


# ── rule 8: reset ────────────────────────────────────────────────────────────

def test_reset_closes_but_keeps_history(cb, clock):
    cb.record_success()
    clock.advance(1)
    _half_open(cb, clock)
    b = cb._BREAKER
    kept = (b.total_failures, b.total_successes, b.last_success,
            b.last_failure, b.opened_at)
    cb.reset()
    assert b.state == "closed" and b.consecutive_failures == 0
    assert b.half_open_probes == 0
    assert (b.total_failures, b.total_successes, b.last_success,
            b.last_failure, b.opened_at) == kept
    assert cb.allow_request() is True and cb.is_online() is True


def test_reset_then_threshold_reopens(cb):
    _open(cb)
    cb.reset()
    cb.record_failure()
    assert cb.status()["state"] == "closed"
    cb.record_failure()
    assert cb.status()["state"] == "open"


# ── rule 9: is_online ────────────────────────────────────────────────────────

def test_is_online_never_spends_the_probe(cb, clock):
    _open(cb)
    clock.advance(COOLDOWN + 5)
    for _ in range(3):
        assert cb.is_online() is False
    assert cb.status()["state"] == "open"
    assert cb._BREAKER.half_open_probes == 0
    assert cb.allow_request() is True


# ── rules 10-11: status ──────────────────────────────────────────────────────

def test_status_shape_and_types(cb, clock):
    _open(cb)
    st = cb.status()
    assert list(st) == ["state", "online", "consecutive_failures",
                        "cooldown_remaining_s", "total_failures", "total_successes",
                        "last_success_ago_s", "last_failure_ago_s"]
    assert isinstance(st["state"], str) and isinstance(st["online"], bool)
    for k in ("consecutive_failures", "cooldown_remaining_s",
              "total_failures", "total_successes", "last_failure_ago_s"):
        assert type(st[k]) is int, k
    assert st["last_success_ago_s"] is None
    json.dumps(st)


def test_status_returns_a_new_dict_and_changes_nothing(cb, clock):
    _open(cb)
    clock.advance(COOLDOWN + 1)
    a = cb.status()
    a["state"] = "tampered"
    b = cb.status()
    assert b["state"] == "open" and a is not b
    assert cb._BREAKER.half_open_probes == 0


def test_cooldown_remaining_counts_down_and_truncates(cb, clock):
    _open(cb)
    assert cb.status()["cooldown_remaining_s"] == 60
    clock.advance(0.1)
    assert cb.status()["cooldown_remaining_s"] == 59     # 59.9 truncated
    clock.advance(30)
    assert cb.status()["cooldown_remaining_s"] == 29
    clock.advance(40)
    assert cb.status()["cooldown_remaining_s"] == 0      # floored, still "open"
    assert cb.status()["state"] == "open"


def test_cooldown_remaining_is_zero_when_not_open(cb, clock):
    cb.record_failure()
    assert cb.status()["cooldown_remaining_s"] == 0
    _half_open(cb, clock)
    assert cb.status()["cooldown_remaining_s"] == 0


def test_ago_values_are_truncated_seconds(cb, clock):
    cb.record_success()
    clock.advance(5)
    cb.record_failure()
    clock.advance(2.9)
    st = cb.status()
    assert st["last_success_ago_s"] == 7      # 7.9 truncated
    assert st["last_failure_ago_s"] == 2      # 2.9 truncated


def test_state_strings_match_constants(cb, clock):
    seen = [cb.status()["state"]]
    _open(cb)
    seen.append(cb.status()["state"])
    clock.advance(COOLDOWN)
    cb.allow_request()
    seen.append(cb.status()["state"])
    assert seen == [cb._CLOSED, cb._OPEN, cb._HALF_OPEN]


# ── rule 12: thread safety ───────────────────────────────────────────────────

def test_concurrent_probe_requests_get_one_true(cb, clock):
    _open(cb)
    clock.advance(COOLDOWN)
    results, start = [], threading.Barrier(16)

    def worker():
        start.wait()
        results.append(cb.allow_request())

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(results) == 16


def test_concurrent_failures_are_all_counted(cb):
    def worker():
        for _ in range(200):
            cb.record_failure()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = cb.status()
    assert st["total_failures"] == 1600 and st["consecutive_failures"] == 1600


def test_lock_is_free_after_every_call(cb, clock):
    _half_open(cb, clock)
    for fn in (cb.allow_request, cb.record_failure, cb.record_success,
               cb.is_online, cb.status, cb.reset):
        fn()
        assert cb._BREAKER.lock.acquire(blocking=False)
        cb._BREAKER.lock.release()


# ── quirks ───────────────────────────────────────────────────────────────────

def test_q1_elapsed_open_breaker_stays_open_until_asked(cb, clock):
    _open(cb)
    clock.advance(10 * COOLDOWN)
    st = cb.status()
    assert st["state"] == "open" and st["online"] is False
    assert st["cooldown_remaining_s"] == 0


def test_q2_repeated_failures_keep_it_open(cb, clock):
    _open(cb)
    for _ in range(5):
        clock.advance(COOLDOWN - 1)
        cb.record_failure()
        assert cb.allow_request() is False
    assert cb.status()["consecutive_failures"] == 7


def test_q7_zero_clock_reads_as_never(cb, clock):
    clock.now = 0.0
    cb.record_success()
    cb.record_failure()
    clock.now = 10.0
    st = cb.status()
    assert st["last_success_ago_s"] is None and st["last_failure_ago_s"] is None
    assert st["total_successes"] == 1 and st["total_failures"] == 1


def test_q8_backwards_clock_is_not_clamped(cb, clock):
    cb.record_failure()
    cb.record_failure()
    clock.advance(-30)
    st = cb.status()
    assert st["last_failure_ago_s"] == -30
    assert st["cooldown_remaining_s"] == 90
    assert cb.allow_request() is False


def test_q10_opened_at_survives_close_and_reset(cb, clock):
    _open(cb)
    opened = cb._BREAKER.opened_at
    clock.advance(5)
    cb.record_success()
    cb.reset()
    assert cb._BREAKER.opened_at == opened


def test_module_has_no_background_work(cb):
    names = {n for n in dir(cb) if not n.startswith("__")}
    assert not any(isinstance(getattr(cb, n), threading.Thread) for n in names)
    assert isinstance(cb.time, Clock)
