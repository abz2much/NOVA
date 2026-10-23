"""Which Decision Record a called-off intrusion marks "wrong".

When intrusions overlap, "the newest intrusion record" can be the wrong one,
so a call-off must judge the exact record of the intrusion it belongs to
(set_outcome by id). Only when no id is known, or that record can no longer
take a verdict, may it fall back to the newest one (set_outcome_recent).

decision_record is replaced by a recorder through sys.modules only, and every
piece of intrusion state a test touches is put back by monkeypatch.
"""
from __future__ import annotations

import sys
import types

import pytest


class Verdicts:
    """Stands in for decision_record and remembers every verdict asked for."""

    def __init__(self, exact_takes=True, explode=False):
        self.exact, self.newest = [], []
        self._exact_takes, self._explode = exact_takes, explode

    def as_module(self):
        mod = types.ModuleType("jc.decision_record")
        mod.set_outcome = self._by_id
        mod.set_outcome_recent = self._newest
        return mod

    def _by_id(self, record_id, verdict, source="", ts=None, db_path=None):
        if self._explode:
            raise OSError("database locked")
        self.exact.append((record_id, verdict, source))
        return self._exact_takes

    def _newest(self, kind, verdict, source="", max_age=3600.0, ts=None, db_path=None):
        self.newest.append((kind, verdict, source, max_age))
        return True


@pytest.fixture
def intr(load, tmp_path, monkeypatch):
    mod = load("intrusion")
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "intrusion_log.json")
    for name, value in (("_log", []), ("_log_loaded", True), ("_called_off_until", 0.0),
                        ("_acknowledged_ts", 0.0), ("_false_alarms", []),
                        ("_last_decision_id", None), ("_last_snapshot", {})):
        monkeypatch.setattr(mod, name, value)
    return mod


@pytest.fixture
def verdicts(monkeypatch):
    def install(**options):
        recorder = Verdicts(**options)
        monkeypatch.setitem(sys.modules, "jc.decision_record", recorder.as_module())
        return recorder
    return install


def test_a_call_off_judges_the_exact_record(intr, verdicts):
    seen = verdicts()
    intr.set_last_decision_id(4242)
    intr.dismiss_intrusion("false alarm")
    assert seen.exact == [(4242, "wrong", "dismiss_intrusion")]
    # The exact id was used, so there is no guessing at the newest record.
    assert seen.newest == []


def test_the_id_is_forgotten_once_judged(intr, verdicts):
    seen = verdicts()
    intr.set_last_decision_id(4242)
    intr.dismiss_intrusion("first")
    assert intr._last_decision_id is None
    # A second call-off with no new intrusion cannot judge 4242 twice.
    intr.dismiss_intrusion("second")
    assert seen.exact == [(4242, "wrong", "dismiss_intrusion")]
    assert [row[:3] for row in seen.newest] == [("intrusion", "wrong", "dismiss_intrusion")]


def test_with_no_id_the_newest_intrusion_is_judged(intr, verdicts):
    seen = verdicts()
    intr.dismiss_intrusion("false alarm")
    assert seen.exact == []
    assert seen.newest == [("intrusion", "wrong", "dismiss_intrusion", intr._CALL_OFF_REACH)]


def test_a_record_that_will_not_take_a_verdict_falls_back(intr, verdicts):
    # The exact record is gone or was judged already.
    seen = verdicts(exact_takes=False)
    intr.set_last_decision_id(99)
    intr.dismiss_intrusion("x")
    assert seen.exact == [(99, "wrong", "dismiss_intrusion")]
    assert [row[:2] for row in seen.newest] == [("intrusion", "wrong")]


def test_a_failing_record_store_never_stops_the_call_off(intr, verdicts):
    verdicts(explode=True)
    intr.set_last_decision_id(5)
    result = intr.dismiss_intrusion("x")
    assert result["ok"] is True
    assert result["suppressed_seconds"] == int(intr._CALLOFF_COOLDOWN)
    assert intr.is_called_off() is True


def test_none_never_replaces_a_known_id(intr):
    intr.set_last_decision_id(None)
    assert intr._last_decision_id is None
    intr.set_last_decision_id(7)
    assert intr._last_decision_id == 7
    intr.set_last_decision_id(None)
    assert intr._last_decision_id == 7


@pytest.mark.parametrize("given,kept", [("12", 12), (12.0, 12), ("abc", 7), ([], 7)])
def test_ids_are_whole_numbers_and_junk_is_ignored(intr, given, kept):
    intr.set_last_decision_id(7)
    intr.set_last_decision_id(given)
    assert intr._last_decision_id == kept
