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
