"""Pin what the decision commands in ws_decisions.py do today (8.7.22, tests only).

nova/root_cause, nova/get_calibration, nova/list_decisions, nova/get_decision,
nova/set_decision_outcome, nova/replay_decision, nova/run_analysis and
nova/get_cognitive_status. nova/set_decision_outcome is the one write: a
verdict feeds calibration and the adaptive thresholds. The decorators are
stubbed to pass through, so the handler bodies run with a recording
connection and fake record, analysis and redaction seams. Tests named
test_current_behaviour_* pin behaviour that looks wrong; each says why.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass
from test_pin_ws_update_config import _Conn, _stub_ws_api


@pytest.fixture
def dec(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    sys.modules.pop("jc.ws_decisions", None)
    mod = load("ws_decisions")
    monkeypatch.setattr(mod, "_entity_names", lambda hass: {"lock.front_door": "Front Door"})
    yield mod
    sys.modules.pop("jc.ws_decisions", None)


@pytest.fixture
def records(load, monkeypatch):
    dr = load("decision_record")
    st = types.SimpleNamespace(rec={"id": 3, "entity_id": "lock.front_door",
                                    "observation": "o" * 600, "api_key": "sk-x"},
                               paged=[], outcomes=[], status="ok")

    def page(limit, kind, only_unjudged, cursor_ts, cursor_id):
        st.paged.append((limit, kind, only_unjudged, cursor_ts, cursor_id))
        return {"items": [{"id": 1, "entity_id": "lock.front_door"}], "next_cursor": None}

    def set_outcome_checked(record_id, verdict, source):
        st.outcomes.append((record_id, verdict, source))
        return st.status
    monkeypatch.setattr(dr, "page", page)
    monkeypatch.setattr(dr, "get", lambda rid: st.rec)
    monkeypatch.setattr(dr, "set_outcome_checked", set_outcome_checked)
    # diagnostics is a package the test loader does not build; a stand-in
    # marks what it redacted.
    diag = types.ModuleType("jc.diagnostics")
    diag._redact = lambda rec: {k: ("**REDACTED**" if k == "api_key" else v)
                                for k, v in rec.items()}
    monkeypatch.setitem(sys.modules, "jc.diagnostics", diag)
    monkeypatch.setattr(sys.modules["jc"], "diagnostics", diag, raising=False)
    return st


async def _call(handler, **msg):
    conn = _Conn()
    await handler(FakeHass(), conn, {"id": 8, **msg})
    return conn


def _raise(*a, **k):
    raise RuntimeError("token=abc")


async def _araise(*a, **k):
    raise RuntimeError("token=abc")


_SAFE = "RuntimeError (details are in the Home Assistant log)"


# ── nova/set_decision_outcome ───────────────────────────────────────────────

@pytest.mark.parametrize("status", ["ok", "already_judged", "not_found"])
async def test_a_verdict_is_recorded_once_from_the_panel(dec, records, status):
    records.status = status
    conn = await _call(dec.ws_set_decision_outcome, decision_id=3, verdict="wrong")
    assert conn.results == [(8, {"status": status})]
    assert records.outcomes == [(3, "wrong", "panel")]


async def test_a_failed_verdict_returns_no_raw_text(dec, records, load, monkeypatch):
    monkeypatch.setattr(load("decision_record"), "set_outcome_checked", _raise)
    conn = await _call(dec.ws_set_decision_outcome, decision_id=3, verdict="good")
    assert conn.errors == [(8, "set_decision_outcome_failed", _SAFE)]


# ── nova/list_decisions and nova/get_decision ──────────────────────────────

@pytest.mark.parametrize("asked,used", [(50, 50), (0, 1), (-1, 1), (10_000, 200)])
async def test_list_decisions_limit_is_held_to_1_to_200(dec, records, asked, used):
    conn = await _call(dec.ws_list_decisions, limit=asked, kind="safety", only_unjudged=True,
                       cursor_ts=2.0, cursor_id=9)
    assert records.paged == [(used, "safety", True, 2.0, 9)]
    assert conn.results[0][1]["next_cursor"] is None
    assert conn.results[0][1]["decisions"][0]["id"] == 1


async def test_get_decision_is_redacted_and_bounded(dec, records):
    conn = await _call(dec.ws_get_decision, decision_id=3)
    d = conn.results[0][1]["decision"]
    assert d["api_key"] == "**REDACTED**"
    assert d["observation"] == "o" * 500 + "…"


async def test_get_and_replay_of_a_missing_decision_is_not_found(dec, records):
    records.rec = None
    for handler in (dec.ws_get_decision, dec.ws_replay_decision):
        conn = await _call(handler, decision_id=99)
        assert conn.errors == [(8, "not_found", "decision not found")]


def test_bounding_keeps_short_values_and_walks_lists(dec):
    out = dec._bound_decision_strings({"a": ["x" * 501, 3, ("y",)], "b": None})
    assert out == {"a": ["x" * 500 + "…", 3, ["y"]], "b": None}


@pytest.mark.parametrize("handler,code", [("ws_list_decisions", "list_decisions_failed"),
                                          ("ws_get_decision", "get_decision_failed"),
                                          ("ws_replay_decision", "replay_decision_failed")])
async def test_record_read_failures_return_no_raw_text(dec, records, load, monkeypatch,
                                                       handler, code):
    dr = load("decision_record")
    monkeypatch.setattr(dr, "page", _raise)
    monkeypatch.setattr(dr, "get", _raise)
    conn = await _call(getattr(dec, handler), decision_id=1, limit=5)
    assert conn.errors == [(8, code, _SAFE)]


async def test_replay_hands_the_record_to_replay_one(dec, records, load, monkeypatch):
    seen = []
    monkeypatch.setattr(load("replay"), "replay_one", lambda rec: seen.append(rec) or {"r": 1})
    conn = await _call(dec.ws_replay_decision, decision_id=3)
    assert conn.results == [(8, {"r": 1})] and seen == [records.rec]


# ── nova/root_cause ─────────────────────────────────────────────────────────

@pytest.fixture
def rca(load, monkeypatch):
    mod = load("rca")
    asked = []
    monkeypatch.setattr(mod, "entity_names", lambda hass: {})
    monkeypatch.setattr(mod, "analyze",
                        lambda eid, t, w, names=None: asked.append((eid, t, w)) or {"ok": 1})
    return mod, asked


async def test_root_cause_uses_the_default_window(dec, rca):
    mod, asked = rca
    conn = await _call(dec.ws_root_cause, entity_id="lock.front_door")
    assert conn.results == [(8, {"ok": 1})]
    assert asked == [("lock.front_door", None, mod.DEFAULT_WINDOW_SECS)]


async def test_current_behaviour_root_cause_window_is_not_bounded(dec, rca):
    # Looks wrong: window_secs goes to the analysis as sent. A ten year
    # window scans every stored event; 0 silently becomes the default and a
    # negative window is passed on.
    mod, asked = rca
    for w in (315_360_000, 0, -60):
        await _call(dec.ws_root_cause, entity_id="lock.front_door", window_secs=w)
    assert [a[2] for a in asked] == [315_360_000, mod.DEFAULT_WINDOW_SECS, -60]


async def test_root_cause_failure_returns_no_raw_text(dec, rca, monkeypatch):
    monkeypatch.setattr(rca[0], "analyze", _raise)
    conn = await _call(dec.ws_root_cause, entity_id="x.y")
    assert conn.errors == [(8, "root_cause_failed", _SAFE)]


# ── calibration, analysis, cognitive status ────────────────────────────────

async def test_calibration_payload(dec, load, monkeypatch):
    dr = load("decision_record")
    monkeypatch.setattr(dr, "calibration", lambda: {"n": 4})
    monkeypatch.setattr(dr, "interruption_budget", lambda: {"judged": 2})
    monkeypatch.setattr(dr, "stats", lambda: {"total": 9})
    monkeypatch.setattr(dr, "outcome_rate", lambda kind, *a: {"kind": kind})
    res = (await _call(dec.ws_get_calibration)).results[0][1]
    assert res["calibration"] == {"n": 4} and res["interruption_budget"] == {"judged": 2}
    assert res["suggestion"] == {"kind": "suggestion"}
    assert res["anticipation"] == {"kind": "anticipation"}


async def test_calibration_failure_is_a_result_with_a_safe_error(dec, load, monkeypatch):
    monkeypatch.setattr(load("decision_record"), "calibration", _raise)
    conn = await _call(dec.ws_get_calibration)
    assert conn.results == [(8, {"calibration": {"n": 0}, "interruption_budget": {"judged": 0},
                                 "error": _SAFE})]


async def test_run_analysis_names_the_diagnostic(dec, load, monkeypatch):
    async def run(hass):
        return {"ran": True, "diagnostic": {"candidates": [{"entity_id": "lock.front_door"}],
                                            "top_sources": [{"x": 1}]}}
    monkeypatch.setattr(load("cognitive_core"), "run_analysis_now", run)
    res = (await _call(dec.ws_run_analysis)).results[0][1]
    assert res["diagnostic"]["candidates"][0]["name"] == "Front Door"
    assert res["diagnostic"]["top_sources"] == [{"x": 1}]


async def test_run_analysis_failure_is_a_result_with_a_safe_error(dec, load, monkeypatch):
    monkeypatch.setattr(load("cognitive_core"), "run_analysis_now", _araise)
    conn = await _call(dec.ws_run_analysis)
    assert conn.results == [(8, {"ran": False, "error": _SAFE})]


async def test_cognitive_status(dec, load, monkeypatch):
    cc = load("cognitive_core")
    monkeypatch.setattr(cc, "status", lambda: {"running": True})
    assert (await _call(dec.ws_get_cognitive_status)).results == [(8, {"running": True})]
    monkeypatch.setattr(cc, "status", _raise)
    assert (await _call(dec.ws_get_cognitive_status)).results == [
        (8, {"running": False, "error": _SAFE, "learning": {}})]
