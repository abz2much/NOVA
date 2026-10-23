"""How often each proactive offer was accepted, and what is saved to disk.

Nova never acts on a proactive offer by itself, so this only counts
acceptances. These pin the saved file, the record fields and the log lines.
"""
import json
import logging

import pytest


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture
def path(cc, tmp_path, monkeypatch):
    p = tmp_path / "grants.json"
    monkeypatch.setattr(cc, "AUTONOMY_FILE", str(p))
    return p


@pytest.fixture
def mgr(cc, path):
    return cc.AutonomyManager()


def _saved(path):
    return json.loads(path.read_text())


# ── recording ────────────────────────────────────────────────────────────────

def test_a_first_acceptance_creates_a_record_and_saves_it(mgr, path):
    rec = mgr.record_acceptance("lights:lounge", confidence=0.6)
    assert set(rec) == {"approvals", "granted", "confidence", "first_seen", "last_accepted"}
    assert rec["approvals"] == 1 and rec["granted"] is False and rec["confidence"] == 0.6
    assert rec["first_seen"] and rec["last_accepted"]
    assert _saved(path) == {"lights:lounge": rec}
    assert path.read_text().startswith('{\n  "lights:lounge"')


def test_the_default_confidence_is_one(mgr):
    assert mgr.record_acceptance("k")["confidence"] == 1.0


def test_later_acceptances_keep_first_seen_and_the_best_confidence(mgr):
    first = dict(mgr.record_acceptance("k", confidence=0.4))
    second = dict(mgr.record_acceptance("k", confidence=0.9))
    third = mgr.record_acceptance("k", confidence=0.2)
    assert second["first_seen"] == first["first_seen"]
    assert (second["approvals"], third["approvals"]) == (2, 3)
    assert third["confidence"] == 0.9


def test_an_accepted_record_is_never_granted_even_if_it_was_saved_that_way(mgr):
    mgr._grants["k"] = {"approvals": 5, "granted": True}
    rec = mgr.record_acceptance("k", confidence=0.5)
    assert rec["granted"] is False and rec["approvals"] == 6
    assert rec["confidence"] == 0.5


@pytest.mark.parametrize("key", ["", None])
def test_no_key_records_nothing_and_saves_nothing(mgr, path, key):
    assert mgr.record_acceptance(key) == {}
    assert not path.exists()


def test_the_record_comes_back_as_the_stored_object(mgr):
    rec = mgr.record_acceptance("k")
    assert rec is mgr._grants["k"]


# ── rejecting and revoking ───────────────────────────────────────────────────

def test_a_rejection_resets_the_count_and_notes_when(mgr, path):
    for _ in range(3):
        mgr.record_acceptance("k", confidence=0.7)
    assert mgr.record_rejection("k") is None
    saved = _saved(path)["k"]
    assert saved["approvals"] == 0 and saved["granted"] is False
    assert saved["last_rejected"] and saved["confidence"] == 0.7


def test_rejecting_an_unknown_key_does_nothing_and_saves_nothing(mgr, path):
    mgr.record_rejection("never")
    assert not path.exists() and mgr.list_grants() == []


def test_a_revoke_resets_the_count_and_notes_when(mgr, path):
    mgr.record_acceptance("k")
    assert mgr.revoke("k") is True
    saved = _saved(path)["k"]
    assert saved["approvals"] == 0 and saved["granted"] is False and saved["revoked_at"]


def test_revoking_an_unknown_key_is_false_and_saves_nothing(mgr, path):
    assert mgr.revoke("never") is False
    assert not path.exists()


@pytest.mark.parametrize("key", ["k", "never", ""])
def test_nothing_is_ever_autonomous(mgr, key):
    mgr.record_acceptance("k")
    assert mgr.is_autonomous(key) is False


# ── listing ──────────────────────────────────────────────────────────────────

def test_the_list_shows_key_count_granted_and_rounded_confidence(mgr):
    mgr.record_acceptance("a", confidence=0.9876)
    mgr.record_acceptance("b", confidence=0.5)
    mgr.record_acceptance("b", confidence=0.5)
    assert mgr.list_grants() == [
        {"pattern_key": "a", "approvals": 1, "granted": False, "confidence": 0.99},
        {"pattern_key": "b", "approvals": 2, "granted": False, "confidence": 0.5}]
    assert list(mgr.list_grants()[0]) == ["pattern_key", "approvals", "granted", "confidence"]


def test_a_record_missing_fields_lists_with_zeros(mgr):
    mgr._grants["bare"] = {}
    assert mgr.list_grants() == [
        {"pattern_key": "bare", "approvals": 0, "granted": False, "confidence": 0.0}]


def test_the_list_always_says_not_granted(mgr):
    mgr._grants["k"] = {"approvals": 4, "granted": True, "confidence": 1}
    assert mgr.list_grants()[0]["granted"] is False


# ── loading ──────────────────────────────────────────────────────────────────

def test_a_missing_file_starts_empty_and_writes_nothing(cc, path):
    assert cc.AutonomyManager()._grants == {}
    assert not path.exists()


def test_saved_counts_load_back(cc, path):
    path.write_text(json.dumps({"k": {"approvals": 2, "granted": False, "confidence": 0.8}}))
    assert cc.AutonomyManager().list_grants() == [
        {"pattern_key": "k", "approvals": 2, "granted": False, "confidence": 0.8}]
    assert _saved(path) == {"k": {"approvals": 2, "granted": False, "confidence": 0.8}}


@pytest.mark.parametrize("text", ["{broken", "[1, 2]", '"text"', "null", "", "5"])
def test_an_unreadable_or_wrong_shaped_file_starts_empty(cc, path, text):
    path.write_text(text)
    assert cc.AutonomyManager()._grants == {}


def test_a_broken_file_is_logged_as_a_warning(cc, path, caplog):
    path.write_text("{broken")
    with caplog.at_level(logging.WARNING, logger="jc.cognitive_core"):
        cc.AutonomyManager()
    assert [r.getMessage().split(":")[0] for r in caplog.records] == [
        "Autonomy grants load failed"]


def test_old_grants_are_cleared_once_and_the_file_is_rewritten(cc, path, caplog):
    path.write_text(json.dumps({
        "old": {"approvals": 3, "granted": True, "confidence": 0.9},
        "off": {"approvals": 1, "granted": False},
        "odd": "not a record"}))
    with caplog.at_level(logging.INFO, logger="jc.cognitive_core"):
        mgr = cc.AutonomyManager()
    saved = _saved(path)
    assert saved["old"]["granted"] is False and saved["old"]["revoked_at"]
    assert saved["old"]["approvals"] == 3
    assert saved["off"] == {"approvals": 1, "granted": False}
    assert saved["odd"] == "not a record"
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "Cleared 1 saved autonomy grant(s): Nova no longer acts on its own")]
    assert mgr._grants["old"]["granted"] is False
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="jc.cognitive_core"):
        cc.AutonomyManager()
    assert caplog.records == []


def test_a_failed_save_is_a_warning_and_the_count_is_kept_in_memory(
        cc, mgr, monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("read only")
    monkeypatch.setattr(cc, "write_json_atomic", boom)
    with caplog.at_level(logging.WARNING, logger="jc.cognitive_core"):
        rec = mgr.record_acceptance("k")
    assert rec["approvals"] == 1 and mgr._grants["k"] is rec
    assert [r.getMessage() for r in caplog.records] == [
        "Autonomy grants save failed: read only"]


def test_the_file_is_written_through_the_patch_point_with_these_options(
        cc, mgr, path, monkeypatch):
    seen = []
    monkeypatch.setattr(cc, "write_json_atomic",
                        lambda target, data, **kw: seen.append((target, dict(data), kw)))
    mgr.record_acceptance("k")
    assert seen[0][0] == str(path) and set(seen[0][1]) == {"k"}
    assert seen[0][2] == {"indent": 2, "encoding": "utf-8"}


def test_an_existing_empty_record_is_counted_on_as_it_is(mgr):
    mgr._grants["k"] = {}
    rec = mgr.record_acceptance("k", confidence=0.3)
    assert set(rec) == {"approvals", "confidence", "last_accepted", "granted"}
    assert rec["approvals"] == 1 and rec["confidence"] == 0.3
