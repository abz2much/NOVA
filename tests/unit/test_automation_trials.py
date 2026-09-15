"""Automation Trials (Phase 3) — probation tracking for installed suggestions.

create()/record_run()/list_trials()/set_manual_outcome() are pure DB
functions, isolated against a tmp-path patterns.db exactly like
decision_record.py's own tests. async_handle_triggered() is exercised
against a fake hass + a fake entity registry, proving the entity-registry
lookup happens synchronously (on the event loop) while the actual write is
handed to async_add_executor_job.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture
def at(load):
    return load("automation_trials")


def test_create_and_list_round_trip(at, tmp_path):
    db = str(tmp_path / "patterns.db")
    tid = at.create(11, "nova_auto_porch_light", db_path=db)
    assert isinstance(tid, int) and tid > 0

    trials = at.list_trials(db_path=db)
    assert len(trials) == 1
    row = trials[0]
    assert row["suggestion_id"] == 11
    assert row["automation_id"] == "nova_auto_porch_light"
    assert row["automation_entity_id"] is None  # resolved lazily, not at creation
    assert row["run_count"] == 0
    assert row["last_run"] is None
    assert row["manual_outcome"] is None


def test_list_trials_empty_db_returns_empty_list(at, tmp_path):
    db = str(tmp_path / "patterns.db")
    assert at.list_trials(db_path=db) == []


def test_list_trials_most_recently_installed_first(at, tmp_path):
    db = str(tmp_path / "patterns.db")
    at.create(1, "nova_auto_a", installed_at=100.0, db_path=db)
    at.create(2, "nova_auto_b", installed_at=200.0, db_path=db)
    trials = at.list_trials(db_path=db)
    assert [t["automation_id"] for t in trials] == ["nova_auto_b", "nova_auto_a"]


# ── record_run — fast path (already resolved) and lazy resolution ──────────

def test_record_run_matches_by_resolved_entity_id(at, tmp_path):
    db = str(tmp_path / "patterns.db")
    tid = at.create(1, "nova_auto_a", db_path=db)
    # simulate a prior resolution
    assert at.record_run("automation.porch_light", "nova_auto_a", ts=100.0, db_path=db)
    row = at.list_trials(db_path=db)[0]
    assert row["run_count"] == 1
    assert row["last_run"] == 100.0
    assert row["automation_entity_id"] == "automation.porch_light"

    # a second run increments further
    assert at.record_run("automation.porch_light", "nova_auto_a", ts=200.0, db_path=db)
    row = at.list_trials(db_path=db)[0]
    assert row["run_count"] == 2
    assert row["last_run"] == 200.0


def test_record_run_resolves_unresolved_trial_by_automation_id(at, tmp_path):
    db = str(tmp_path / "patterns.db")
    at.create(1, "nova_auto_a", db_path=db)  # automation_entity_id starts NULL
    updated = at.record_run("automation.porch_light", "nova_auto_a", ts=100.0, db_path=db)
    assert updated is True
    row = at.list_trials(db_path=db)[0]
    assert row["automation_entity_id"] == "automation.porch_light"
    assert row["run_count"] == 1


def test_record_run_no_match_for_unrelated_automation(at, tmp_path):
    """Every automation in the house fires this event, not just Nova's — a
    miss must be silent, not an error, and must not touch unrelated rows."""
    db = str(tmp_path / "patterns.db")
    at.create(1, "nova_auto_a", db_path=db)
    updated = at.record_run("automation.someone_elses_automation", "not_nova_at_all", db_path=db)
    assert updated is False
    row = at.list_trials(db_path=db)[0]
    assert row["run_count"] == 0
    assert row["automation_entity_id"] is None


# ── manual feedback — not set-once ──────────────────────────────────────────

def test_set_manual_outcome_working(at, tmp_path):
    db = str(tmp_path / "patterns.db")
    tid = at.create(1, "nova_auto_a", db_path=db)
    assert at.set_manual_outcome(tid, "working", db_path=db) is True
    row = at.list_trials(db_path=db)[0]
    assert row["manual_outcome"] == "working"
    assert row["manual_outcome_ts"] is not None


def test_set_manual_outcome_can_change_later(at, tmp_path):
    """Unlike a Decision Record's immutable outcome, the household's own
    assessment may legitimately change — this is not set-once."""
    db = str(tmp_path / "patterns.db")
    tid = at.create(1, "nova_auto_a", db_path=db)
    at.set_manual_outcome(tid, "needs_adjustment", db_path=db)
    assert at.set_manual_outcome(tid, "working", db_path=db) is True
    row = at.list_trials(db_path=db)[0]
    assert row["manual_outcome"] == "working"


def test_set_manual_outcome_missing_trial_is_false(at, tmp_path):
    db = str(tmp_path / "patterns.db")
    assert at.set_manual_outcome(99999, "working", db_path=db) is False


# ── installing does not imply performance ───────────────────────────────────

def test_create_never_sets_manual_outcome(at, tmp_path):
    """Installation is acceptance, not proof the automation works — a fresh
    trial must start with no manual verdict at all."""
    db = str(tmp_path / "patterns.db")
    at.create(1, "nova_auto_a", db_path=db)
    row = at.list_trials(db_path=db)[0]
    assert row["manual_outcome"] is None
    assert row["run_count"] == 0


# ── idempotent schema — preserves existing patterns.db data ────────────────

def test_schema_creation_preserves_existing_tables(at, tmp_path):
    import sqlite3
    db = str(tmp_path / "patterns.db")
    # Simulate an existing patterns.db with unrelated data already in it,
    # matching cognitive_core.StateLogger's own tables.
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE state_changes (id INTEGER PRIMARY KEY, entity_id TEXT)")
    conn.execute("INSERT INTO state_changes (entity_id) VALUES ('light.porch')")
    conn.commit()
    conn.close()

    at.create(1, "nova_auto_a", db_path=db)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT entity_id FROM state_changes").fetchall()
    conn.close()
    assert rows == [("light.porch",)]  # untouched by the new table's creation


# ── async_handle_triggered — registry lookup on the loop, write off it ─────

class _FakeEntry:
    def __init__(self, unique_id):
        self.unique_id = unique_id


class _FakeRegistry:
    def __init__(self, entries: dict):
        self._entries = entries

    def async_get(self, entity_id):
        return self._entries.get(entity_id)


class _FakeEvent:
    def __init__(self, entity_id, time_fired=None):
        self.data = {"entity_id": entity_id} if entity_id else {}
        self.time_fired = time_fired


def _install_fake_entity_registry(monkeypatch, entries: dict):
    import sys
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: _FakeRegistry(entries))


async def test_async_handle_triggered_records_via_executor(at, fake_hass, monkeypatch):
    _install_fake_entity_registry(monkeypatch, {
        "automation.porch_light": _FakeEntry("nova_auto_a"),
    })
    calls = []
    monkeypatch.setattr(at, "record_run", lambda *a, **k: calls.append((a, k)) or True)

    await at.async_handle_triggered(fake_hass, _FakeEvent("automation.porch_light"))

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "automation.porch_light"
    assert args[1] == "nova_auto_a"


async def test_async_handle_triggered_ignores_event_without_entity_id(at, fake_hass, monkeypatch):
    calls = []
    monkeypatch.setattr(at, "record_run", lambda *a, **k: calls.append(1) or True)
    await at.async_handle_triggered(fake_hass, _FakeEvent(None))
    assert calls == []


async def test_async_handle_triggered_survives_registry_lookup_failure(at, fake_hass, monkeypatch):
    """A registry error must not stop the run from being recorded — it just
    means unique_id resolution falls back to None (no lazy resolve this time)."""
    import sys
    er = sys.modules["homeassistant.helpers.entity_registry"]

    def _boom(hass):
        raise RuntimeError("registry unavailable")
    monkeypatch.setattr(er, "async_get", _boom)

    calls = []
    monkeypatch.setattr(at, "record_run", lambda *a, **k: calls.append(a) or True)
    await at.async_handle_triggered(fake_hass, _FakeEvent("automation.porch_light"))
    assert calls and calls[0][1] is None
