"""Tests for the Action Audit Log core module (action_log.py).

Modeled directly on spoken_history.py's own test conventions — every entry
point takes db_path explicitly so tests run fully isolated from any real
Nova install. Covers: storage/retention (group-aware), conditional/
idempotent state transitions, concurrent updates to separate rows,
unwritable-database fail-open behavior, and request-level keyset
pagination (never splitting one request's targets across two pages).
"""
from __future__ import annotations

import os
import tempfile
import threading

import pytest


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let action_log create it fresh
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass


# ── storage ───────────────────────────────────────────────────────────────

def test_start_returns_an_id_and_defaults_are_correct(al, db_path):
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", domain="light",
                    entity_id="light.kitchen", db_path=db_path)
    assert isinstance(aid, int)
    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["approval_required"] == 0
    assert got["targets"][0]["approval_result"] == "not_required"
    assert got["targets"][0]["execution_result"] == "pending"


def test_start_many_inserts_every_target_in_one_call(al, db_path):
    rid = al.new_request_id()
    targets = [{"entity_id": f"light.{i}"} for i in range(6)]
    ids = al.start_many(rid, "bulk_control", "voice", targets, db_path=db_path)
    assert len(ids) == 6
    got = al.get_request(rid, db_path=db_path)
    assert len(got["targets"]) == 6


def test_start_many_empty_targets_returns_empty_mapping(al, db_path):
    rid = al.new_request_id()
    assert al.start_many(rid, "bulk_control", "voice", [], db_path=db_path) == {}


# ── conditional / idempotent updates ─────────────────────────────────────

def test_set_execution_moves_pending_to_accepted_to_verified(al, db_path):
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)
    assert al.set_execution(aid, "accepted", db_path=db_path) is True
    assert al.set_execution(aid, "verified", db_path=db_path) is True
    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["execution_result"] == "verified"


def test_set_execution_cannot_skip_accepted(al, db_path):
    """verified/unverified require accepted first — a straight
    pending -> verified jump is rejected, not silently allowed."""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)
    assert al.set_execution(aid, "verified", db_path=db_path) is False
    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["execution_result"] == "pending"


def test_duplicate_or_late_verification_callback_cannot_overwrite_terminal(al, db_path):
    """The exact scenario a race between two verification attempts must
    not produce: once a row reaches 'verified', a late 'unverified'
    callback (or a duplicate 'verified' one) must no-op, never flip it."""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)
    al.set_execution(aid, "accepted", db_path=db_path)
    assert al.set_execution(aid, "verified", db_path=db_path) is True
    # Late/duplicate callback arrives after the row is already terminal.
    assert al.set_execution(aid, "unverified", db_path=db_path) is False
    assert al.set_execution(aid, "verified", db_path=db_path) is False
    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["execution_result"] == "verified"


def test_mark_awaiting_approval_moves_default_to_awaiting_once(al, db_path):
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)  # default: not_required
    assert al.mark_awaiting_approval(aid, db_path=db_path) is True
    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["approval_result"] == "awaiting"
    assert got["targets"][0]["approval_required"] == 1
    # A second call must no-op -- it's not a re-enterable transition.
    assert al.mark_awaiting_approval(aid, db_path=db_path) is False


def test_mark_awaiting_approval_noops_on_none(al, db_path):
    assert al.mark_awaiting_approval(None, db_path=db_path) is False


def test_set_approval_moves_awaiting_to_terminal_exactly_once(al, db_path):
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice",
                    approval_required=True, approval_result="awaiting", db_path=db_path)
    assert al.set_approval(aid, "approved", approval_required=True, db_path=db_path) is True
    # A second, duplicate resolution must no-op.
    assert al.set_approval(aid, "rejected", db_path=db_path) is False
    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["approval_result"] == "approved"
    assert got["targets"][0]["approval_required"] == 1


def test_set_approval_awaiting_to_not_required_is_permitted(al, db_path):
    """confirm_gate can determine "not protected" only after being asked —
    this is a legitimate exit from awaiting, not a re-entry into it."""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice",
                    approval_required=True, approval_result="awaiting", db_path=db_path)
    assert al.set_approval(aid, "not_required", approval_required=False, db_path=db_path) is True
    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["approval_result"] == "not_required"
    assert got["targets"][0]["approval_required"] == 0


@pytest.mark.parametrize("value", ["not_requested", "awaiting"])
def test_set_approval_rejects_creation_only_values(al, db_path, value):
    """not_requested/awaiting are only ever written at row creation — a
    mutator call with one of them is not a legal transition. ("not_required"
    is the one exception: confirm_gate can only determine "not protected"
    after being asked, so awaiting -> not_required is a legitimate exit,
    covered separately below.)"""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice",
                    approval_required=True, approval_result="awaiting", db_path=db_path)
    assert al.set_approval(aid, value, db_path=db_path) is False


# ── action_id=None safety ────────────────────────────────────────────────

def test_mutators_noop_safely_on_none_action_id(al, db_path):
    assert al.set_approval(None, "approved", db_path=db_path) is False
    assert al.set_execution(None, "accepted", db_path=db_path) is False


def test_start_on_unwritable_database_returns_none(al, monkeypatch):
    def _boom(db_path):
        raise OSError("Read-only file system")
    monkeypatch.setattr(al, "_connect", _boom)
    rid = al.new_request_id()
    assert al.start(rid, "control_device", "voice", db_path="/no/such/path.db") is None
    assert al.start_many(rid, "bulk_control", "voice", [{"entity_id": "light.x"}],
                          db_path="/no/such/path.db") == {}
    assert al.set_execution(1, "accepted", db_path="/no/such/path.db") is False


# ── retention: group-aware ───────────────────────────────────────────────

def test_retention_keeps_or_drops_whole_requests_never_partial(al, db_path, monkeypatch):
    monkeypatch.setattr(al, "_KEEP_REQUESTS", 2)
    rid1 = al.new_request_id()
    al.start_many(rid1, "bulk_control", "voice",
                   [{"entity_id": "light.a"}, {"entity_id": "light.b"}, {"entity_id": "light.c"}],
                   db_path=db_path)
    rid2 = al.new_request_id()
    al.start(rid2, "control_device", "voice", db_path=db_path)
    rid3 = al.new_request_id()
    al.start(rid3, "control_device", "voice", db_path=db_path)

    # rid1 (the oldest) should be pruned entirely — either all 3 of its rows
    # survive or none do, never 1 or 2 of them.
    got1 = al.get_request(rid1, db_path=db_path)
    got2 = al.get_request(rid2, db_path=db_path)
    got3 = al.get_request(rid3, db_path=db_path)
    assert got1 is None
    assert got2 is not None and got3 is not None


# ── concurrency ───────────────────────────────────────────────────────────

def test_concurrent_updates_to_separate_rows_are_all_applied(al, db_path):
    rid = al.new_request_id()
    ids = al.start_many(
        rid, "bulk_control", "voice",
        [{"entity_id": f"light.{i}"} for i in range(20)],
        db_path=db_path,
    )
    assert len(ids) == 20

    results = {}
    def _accept(key, aid):
        results[key] = al.set_execution(aid, "accepted", db_path=db_path)

    threads = [threading.Thread(target=_accept, args=(k, v)) for k, v in ids.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(results.values()), results
    got = al.get_request(rid, db_path=db_path)
    assert all(t["execution_result"] == "accepted" for t in got["targets"])


# ── request-level pagination ─────────────────────────────────────────────

def test_page_requests_never_splits_one_request_across_pages(al, db_path):
    """A single bulk request with MORE targets than the page limit is still
    returned as one complete group on one page."""
    rid_big = al.new_request_id()
    al.start_many(
        rid_big, "bulk_control", "voice",
        [{"entity_id": f"light.{i}"} for i in range(10)],
        db_path=db_path,
    )
    page = al.page_requests(limit=3, db_path=db_path)
    matching = [r for r in page["requests"] if r["request_id"] == rid_big]
    assert len(matching) == 1
    assert len(matching[0]["targets"]) == 10


def test_page_requests_paginates_newest_first_by_keyset(al, db_path):
    import time
    rids = []
    for i in range(5):
        rid = al.new_request_id()
        al.start(rid, "control_device", "voice", db_path=db_path)
        rids.append(rid)
        time.sleep(0.002)  # ensure distinct ts_created ordering

    page1 = al.page_requests(limit=2, db_path=db_path)
    assert [r["request_id"] for r in page1["requests"]] == list(reversed(rids))[:2]
    assert page1["next_cursor"] is not None

    page2 = al.page_requests(
        limit=2,
        cursor_ts=page1["next_cursor"]["ts"],
        cursor_request_id=page1["next_cursor"]["request_id"],
        db_path=db_path,
    )
    seen = [r["request_id"] for r in page1["requests"]] + [r["request_id"] for r in page2["requests"]]
    assert len(set(seen)) == len(seen)  # no request repeated across pages
    assert set(seen) <= set(rids)


def test_page_requests_aggregate_status(al, db_path):
    rid = al.new_request_id()
    ids = al.start_many(
        rid, "bulk_control", "voice",
        [{"entity_id": "light.a"}, {"entity_id": "light.b"}],
        db_path=db_path,
    )
    al.set_execution(list(ids.values())[0], "accepted", db_path=db_path)
    al.set_execution(list(ids.values())[0], "verified", db_path=db_path)
    al.set_execution(list(ids.values())[1], "failed", db_path=db_path)
    page = al.page_requests(limit=10, db_path=db_path)
    row = next(r for r in page["requests"] if r["request_id"] == rid)
    assert row["status"] == "partial"  # one verified, one failed -> mixed, not full success/failure


# ── bounded SQLITE_BUSY/SQLITE_LOCKED retry ──────────────────────────────
#
# All of these drive the retry path through controlled fault injection
# (a fake _connect that raises a real sqlite3.OperationalError with a real
# SQLITE_BUSY/SQLITE_LOCKED errorcode on chosen calls) rather than timing
# races, so they are deterministic. `no_retry_delay` collapses the retry
# helper's backoff to zero real time so the suite stays fast; it does not
# change attempt counts or which errors are retried.

def _busy_error(msg: str = "database is locked") -> "sqlite3.OperationalError":
    import sqlite3
    exc = sqlite3.OperationalError(msg)
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    exc.sqlite_errorname = "SQLITE_BUSY"
    return exc


def _permanent_error(msg: str = "unable to open database file") -> "sqlite3.OperationalError":
    import sqlite3
    exc = sqlite3.OperationalError(msg)
    # A real permanent failure's errorcode is never BUSY/LOCKED.
    exc.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    exc.sqlite_errorname = "SQLITE_CANTOPEN"
    return exc


@pytest.fixture
def no_retry_delay(al, monkeypatch):
    """Make _run_with_retry's backoff instant and deterministic: no real
    sleep, and jitter always returns the low end of its (low, high) range."""
    monkeypatch.setattr(al, "_RETRY_SLEEP", lambda seconds: None)
    monkeypatch.setattr(al, "_RETRY_JITTER", lambda low, high: low)


def test_is_transient_busy_error_classifies_by_sqlite_errorcode(al):
    assert al._is_transient_busy_error(_busy_error()) is True
    locked = _busy_error("database table is locked")
    locked.sqlite_errorcode = al.sqlite3.SQLITE_LOCKED
    assert al._is_transient_busy_error(locked) is True
    assert al._is_transient_busy_error(_permanent_error()) is False
    assert al._is_transient_busy_error(ValueError("not sqlite at all")) is False


def test_is_transient_busy_error_recognises_extended_busy_and_locked_codes(al):
    """SQLite packs the primary result code into the low 8 bits of an
    extended code and puts extra detail above that (e.g. SQLITE_BUSY_TIMEOUT
    = SQLITE_BUSY | (3 << 8), SQLITE_LOCKED_SHAREDCACHE = SQLITE_LOCKED |
    (1 << 8)) — the classifier must mask down to the primary code before
    comparing, not require an exact match against the bare primary value."""
    busy_timeout = _busy_error("database is locked")
    busy_timeout.sqlite_errorcode = al.sqlite3.SQLITE_BUSY | (3 << 8)
    assert al._is_transient_busy_error(busy_timeout) is True

    busy_recovery = _busy_error("database is locked")
    busy_recovery.sqlite_errorcode = al.sqlite3.SQLITE_BUSY | (1 << 8)
    assert al._is_transient_busy_error(busy_recovery) is True

    locked_sharedcache = _busy_error("database table is locked")
    locked_sharedcache.sqlite_errorcode = al.sqlite3.SQLITE_LOCKED | (1 << 8)
    assert al._is_transient_busy_error(locked_sharedcache) is True

    locked_vtab = _busy_error("database table is locked")
    locked_vtab.sqlite_errorcode = al.sqlite3.SQLITE_LOCKED | (2 << 8)
    assert al._is_transient_busy_error(locked_vtab) is True

    # An extended code whose PRIMARY (low 8 bits) is neither BUSY nor LOCKED
    # must still be treated as permanent, extended form or not.
    cantopen_extended = _permanent_error()
    cantopen_extended.sqlite_errorcode = al.sqlite3.SQLITE_CANTOPEN | (2 << 8)
    assert al._is_transient_busy_error(cantopen_extended) is False


def test_is_transient_busy_error_falls_back_to_message_without_errorcode(al):
    """Compatibility path only — a real Python 3.11+ error always carries
    sqlite_errorcode, but the classifier must still work if it's absent."""
    exc = al.sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = None
    assert al._is_transient_busy_error(exc) is True
    exc2 = al.sqlite3.OperationalError("unable to open database file")
    exc2.sqlite_errorcode = None
    assert al._is_transient_busy_error(exc2) is False


def test_transient_lock_succeeds_after_retry(al, db_path, monkeypatch, no_retry_delay):
    """A single SQLITE_BUSY on the first attempt must not fail the write —
    the second (of three) attempts, on a fresh connection, succeeds. Fails
    against the pre-fix implementation, which had no outer retry at all and
    would return False/None on the very first busy error."""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)
    al.set_execution(aid, "accepted", db_path=db_path)

    real_connect = al._connect
    calls = {"n": 0}

    def flaky_connect(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _busy_error()
        return real_connect(path)

    monkeypatch.setattr(al, "_connect", flaky_connect)
    assert al.set_execution(aid, "verified", db_path=db_path) is True
    assert calls["n"] == 2  # one failed attempt, one successful retry — not 3

    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["execution_result"] == "verified"


def test_retry_exhaustion_fails_open_without_raising(al, db_path, monkeypatch, no_retry_delay):
    """Sustained SQLITE_BUSY across all three attempts must still fail open
    (return False, never raise) and must not touch the row. Fails against
    the pre-fix implementation only in attempt count (it fails open after a
    single attempt) — this proves the NEW implementation spends its full,
    but still bounded, budget rather than giving up early or retrying
    forever."""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)
    al.set_execution(aid, "accepted", db_path=db_path)

    calls = {"n": 0}

    def always_busy(path):
        calls["n"] += 1
        raise _busy_error()

    monkeypatch.setattr(al, "_connect", always_busy)
    assert al.set_execution(aid, "verified", db_path=db_path) is False
    assert calls["n"] == al._MAX_ATTEMPTS == 3
    monkeypatch.undo()  # restore the real _connect before reading back to verify

    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["execution_result"] == "accepted"  # unchanged, not corrupted


def test_permanent_database_error_is_not_retried(al, db_path, monkeypatch, no_retry_delay):
    """A non-transient OperationalError (bad file, corruption, permissions —
    anything that isn't SQLITE_BUSY/SQLITE_LOCKED) must fail open on the
    FIRST attempt. Retrying it would just waste the bounded budget on a
    fault no retry can fix."""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)

    calls = {"n": 0}

    def boom(path):
        calls["n"] += 1
        raise _permanent_error()

    monkeypatch.setattr(al, "_connect", boom)
    assert al.set_execution(aid, "accepted", db_path=db_path) is False
    assert calls["n"] == 1


def test_start_many_permanent_error_is_not_retried(al, monkeypatch):
    calls = {"n": 0}

    def boom(path):
        calls["n"] += 1
        raise _permanent_error()

    monkeypatch.setattr(al, "_connect", boom)
    rid = al.new_request_id()
    assert al.start_many(
        rid, "bulk_control", "voice", [{"entity_id": "light.x"}],
    ) == {}
    assert calls["n"] == 1


def test_no_duplicate_rows_after_start_many_retries(al, db_path, monkeypatch, no_retry_delay):
    """The first attempt inserts 2 of 5 targets into its own (uncommitted)
    transaction, then hits a transient lock on the 3rd insert — that whole
    attempt must be discarded, not partially kept, so the retry's full
    re-insert leaves exactly 5 rows, never 7. Fails against the pre-fix
    implementation because there IS no retry: the first attempt's failure
    is final, so this scenario returns {} with the transaction rolled back
    and NO rows persisted at all — the assertion on row count (5) fails."""
    class _FlakyConn:
        """sqlite3.Connection has no instance __dict__ (a C type), so its
        methods can't be monkeypatched directly — wrap it instead and
        forward everything except a deliberately-flaky execute()."""
        def __init__(self, real):
            self._real = real
            self._insert_n = 0

        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("INSERT"):
                self._insert_n += 1
                if self._insert_n == 3:
                    raise _busy_error()
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = al._connect
    attempt_n = {"n": 0}

    def flaky_connect(path):
        attempt_n["n"] += 1
        conn = real_connect(path)
        return _FlakyConn(conn) if attempt_n["n"] == 1 else conn

    monkeypatch.setattr(al, "_connect", flaky_connect)
    rid = al.new_request_id()
    targets = [{"entity_id": f"light.{i}"} for i in range(5)]
    ids = al.start_many(rid, "bulk_control", "voice", targets, db_path=db_path)

    assert len(ids) == 5
    assert attempt_n["n"] == 2  # one aborted attempt, one clean retry
    got = al.get_request(rid, db_path=db_path)
    assert len(got["targets"]) == 5  # not 7 — the aborted attempt left nothing behind


def test_stale_update_cannot_overwrite_newer_terminal_state_even_after_retry(
    al, db_path, monkeypatch, no_retry_delay,
):
    """The exact race the CAS guard exists for, but now driven through the
    retry path: a late 'unverified' callback hits one transient lock,
    retries, and STILL correctly no-ops on its retry because by then the
    row is already 'verified' — the guarded UPDATE's WHERE clause is
    re-evaluated fresh on every attempt, so a retry can never resurrect a
    transition that's no longer legal."""
    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", db_path=db_path)
    al.set_execution(aid, "accepted", db_path=db_path)
    assert al.set_execution(aid, "verified", db_path=db_path) is True

    real_connect = al._connect
    calls = {"n": 0}

    def flaky_connect(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _busy_error()
        return real_connect(path)

    monkeypatch.setattr(al, "_connect", flaky_connect)
    # A late/duplicate "unverified" callback arrives after the row already
    # reached its terminal "verified" state, AND hits contention on its
    # first attempt.
    assert al.set_execution(aid, "unverified", db_path=db_path) is False
    assert calls["n"] == 2  # it did retry — the no-op is from the guard, not a skipped attempt

    got = al.get_request(rid, db_path=db_path)
    assert got["targets"][0]["execution_result"] == "verified"  # never overwritten
