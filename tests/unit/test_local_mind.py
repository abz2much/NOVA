"""Fresh behaviour tests for local_mind.py: the offline decision façade
(history grounding, flap memory, case prior, counters) and the voice every
local announcement is composed through.

Persona variety is switched off so openers are the first variant of each
register; every module-level cache is reset per test."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
import types
from collections import deque

import pytest


@pytest.fixture
def lm(load, monkeypatch, tmp_path):
    m = load("local_mind")
    persona = load("persona")
    monkeypatch.setattr(persona, "_VARIETY", False)
    monkeypatch.setattr(persona, "_recent", {})
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "absent_dir" / "patterns.db"))
    monkeypatch.setattr(m, "_hist_cache", {})
    monkeypatch.setattr(m, "_days_cache", (0.0, 0.0))
    monkeypatch.setattr(m, "_recent_events", {})
    monkeypatch.setattr(m, "_stats", {"decisions": 0, "spoke": 0, "silent": 0})
    return m


def _make_db(path, rows, span_days=10):
    """rows: (entity_id, new_state, hour). Two marker rows set the span.
    Any earlier database at `path` is replaced."""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE state_changes (id INTEGER PRIMARY KEY, timestamp TEXT, "
                 "entity_id TEXT, new_state TEXT, hour INTEGER)")
    conn.execute("INSERT INTO state_changes (timestamp, entity_id, new_state, hour) "
                 "VALUES ('2026-01-01 00:00:00', 'sensor.marker', 'x', 0)")
    conn.execute("INSERT INTO state_changes (timestamp, entity_id, new_state, hour) "
                 "VALUES (datetime('2026-01-01 00:00:00', ?), 'sensor.marker', 'x', 0)",
                 (f"+{span_days} days",))
    for eid, state, hour in rows:
        conn.execute("INSERT INTO state_changes (timestamp, entity_id, new_state, hour) "
                     "VALUES ('2026-01-02 00:00:00', ?, ?, ?)", (eid, state, hour))
    conn.commit()
    conn.close()


@pytest.fixture
def db(lm, tmp_path, monkeypatch):
    path = tmp_path / "patterns.db"

    def build(rows, span_days=10):
        _make_db(str(path), rows, span_days)
        monkeypatch.setattr(lm, "DB_PATH", str(path))
    return build


DOOR = "binary_sensor.kitchen_door"


def _log_to(monkeypatch, fn):
    """Route websocket.nova_log to `fn` (a stand-in websocket module, since
    other tests may have removed or replaced the shared stub)."""
    ws = types.ModuleType("jc.websocket")
    ws.nova_log = fn
    monkeypatch.setitem(sys.modules, "jc.websocket", ws)


# ── Rule: constants & module state ─────────────────────────────────────────

def test_constants(lm):
    assert lm._HIST_TTL == 600.0 and lm._DAYS_TTL == 3600.0
    assert lm._FLAP_WINDOW == 300.0 and lm._FLAP_COUNT == 3


# ── _connect / _data_days ──────────────────────────────────────────────────

def test_connect_missing_directory_is_none(lm):
    assert lm._connect() is None


def test_data_days_counts_span(lm, db):
    db([], span_days=7)
    assert lm._data_days() == pytest.approx(7.0)


def test_data_days_is_cached_for_an_hour(lm, db, monkeypatch):
    db([], span_days=7)
    assert lm._data_days() == pytest.approx(7.0)
    assert lm._days_cache[0] > time.monotonic() + 3500
    db([], span_days=1)                     # db changes, cached value stays
    assert lm._data_days() == pytest.approx(7.0)


def test_data_days_without_database_is_zero_and_cached(lm):
    assert lm._data_days() == 0.0
    assert lm._days_cache[1] == 0.0 and lm._days_cache[0] > time.monotonic()


def test_data_days_empty_table_is_zero(lm, tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE state_changes (timestamp TEXT)")
    conn.close()
    monkeypatch.setattr(lm, "DB_PATH", str(path))
    assert lm._data_days() == 0.0


# ── history_profile grades ─────────────────────────────────────────────────

def test_profile_unknown_without_history(lm):
    assert lm.history_profile(DOOR, "on", 14) == {
        "days": 0.0, "at_hour": 0, "total": 0, "grade": "unknown"}


def test_profile_unknown_under_three_days(lm, db):
    db([(DOOR, "on", 14)] * 5, span_days=2)
    out = lm.history_profile(DOOR, "on", 14)
    assert out == {"days": 2.0, "at_hour": 0, "total": 0, "grade": "unknown"}


def test_profile_novel(lm, db):
    db([(DOOR, "off", 14)])
    assert lm.history_profile(DOOR, "on", 14)["grade"] == "novel"


def test_profile_unusual_hour(lm, db):
    db([(DOOR, "on", 3), (DOOR, "on", 4)])
    out = lm.history_profile(DOOR, "on", 14)
    assert out == {"days": 10.0, "at_hour": 0, "total": 2, "grade": "unusual_hour"}


@pytest.mark.parametrize("n,grade", [(1, "occasional"), (2, "common"), (4, "common"),
                                     (5, "routine"), (9, "routine")])
def test_profile_rate_grades(lm, db, n, grade):
    db([(DOOR, "on", 14)] * n, span_days=10)
    out = lm.history_profile(DOOR, "on", 14)
    assert out["grade"] == grade and out["at_hour"] == n and out["total"] == n


def test_profile_counts_neighbouring_hours_and_wraps(lm, db):
    db([(DOOR, "on", 23), (DOOR, "on", 0), (DOOR, "on", 1), (DOOR, "on", 2)])
    out = lm.history_profile(DOOR, "on", 0)
    assert out["at_hour"] == 3 and out["total"] == 4


def test_profile_matches_state_as_text(lm, db):
    db([("sensor.remote_battery", "5", 14)] * 6)
    assert lm.history_profile("sensor.remote_battery", 5, 14)["grade"] == "routine"


def test_profile_days_rounded(lm, db):
    db([], span_days=3.26)
    assert lm.history_profile(DOOR, "on", 1)["days"] == 3.3


def test_profile_is_cached_per_entity_state_hour(lm, db):
    db([(DOOR, "on", 14)])
    first = lm.history_profile(DOOR, "on", 14)
    db([(DOOR, "on", 14)] * 9)
    lm._days_cache = (0.0, 0.0)
    assert lm.history_profile(DOOR, "on", 14) == first
    assert lm.history_profile(DOOR, "on", 15)["at_hour"] == 9  # different key


def test_profile_cache_expiry_refetches(lm, db):
    db([(DOOR, "on", 14)])
    lm.history_profile(DOOR, "on", 14)
    key = (DOOR, "on", 14)
    lm._hist_cache[key] = (time.monotonic() - 1, lm._hist_cache[key][1])
    db([(DOOR, "on", 14)] * 9)
    assert lm.history_profile(DOOR, "on", 14)["at_hour"] == 9


def test_profile_cache_is_trimmed(lm):
    far = time.monotonic() + 10_000
    for i in range(800):
        lm._hist_cache[(f"sensor.s{i}", "on", 1)] = (far, {})
    lm.history_profile("sensor.new", "on", 1)
    assert len(lm._hist_cache) == 601
    assert ("sensor.s0", "on", 1) not in lm._hist_cache
    assert ("sensor.s199", "on", 1) not in lm._hist_cache
    assert ("sensor.s200", "on", 1) in lm._hist_cache
    assert ("sensor.new", "on", 1) in lm._hist_cache


def test_profile_query_failure_keeps_unknown(lm, tmp_path, monkeypatch):
    path = tmp_path / "odd.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE state_changes (timestamp TEXT)")   # no entity/hour columns
    conn.execute("INSERT INTO state_changes VALUES ('2026-01-01'), ('2026-01-20')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(lm, "DB_PATH", str(path))
    out = lm.history_profile(DOOR, "on", 14)
    assert out["grade"] == "unknown" and out["days"] == 19.0


def test_connect_is_looked_up_at_call_time(lm, monkeypatch):
    monkeypatch.setattr(lm, "_connect", lambda: None)
    assert lm.history_profile(DOOR, "on", 14)["grade"] == "unknown"


# ── flap memory ────────────────────────────────────────────────────────────

def test_third_event_in_window_is_flapping(lm):
    assert lm._note_event("switch.pump") is False
    assert lm._note_event("switch.pump") is False
    assert lm._note_event("switch.pump") is True
    assert lm._note_event("switch.other") is False


def test_old_events_age_out(lm):
    old = time.monotonic() - 301
    lm._recent_events["switch.pump"] = deque([old, old, old], maxlen=8)
    assert lm._note_event("switch.pump") is False


def test_flap_memory_is_bounded(lm):
    for _ in range(20):
        lm._note_event("switch.pump")
    assert len(lm._recent_events["switch.pump"]) == 8


# ── duplicate check & case prior ───────────────────────────────────────────

def test_is_duplicate_by_name_or_id(lm):
    assert lm._is_duplicate("Kitchen Door", DOOR, ["Sir, kitchen door is open."])
    assert lm._is_duplicate("", DOOR, ["the kitchen door again"])
    assert not lm._is_duplicate("Kitchen Door", DOOR, ["Sir, the hall light is on."])
    assert not lm._is_duplicate("", "", ["anything"])


def test_is_duplicate_only_looks_at_the_last_six(lm):
    recent = ["kitchen door"] + ["other"] * 6
    assert not lm._is_duplicate("Kitchen Door", "", recent)


def test_case_prior_reads_the_cache(lm, load, monkeypatch):
    rc = load("reasoning_cache")
    seen = []
    monkeypatch.setattr(rc, "similar", lambda *a: seen.append(a) or (3, 1))
    assert lm._case_prior("binary_sensor", "door", "doors_windows", True) == (3, 1)
    assert seen == [("binary_sensor", "door", "doors_windows", True)]


def test_case_prior_failure_is_zero(lm, load, monkeypatch):
    rc = load("reasoning_cache")

    def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(rc, "similar", boom)
    assert lm._case_prior("a", "b", "c", False) == (0, 0)


def test_ev_is_the_evaluators_module(lm, load):
    assert lm._ev() is load("cognitive.evaluators")


# ── _state_phrase ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("to,dc,eid,phrase", [
    ("on", "smoke", "binary_sensor.a", "is detecting smoke"),
    ("on", "carbon_monoxide", "binary_sensor.a", "is detecting carbon monoxide"),
    ("on", "gas", "binary_sensor.a", "is detecting gas"),
    ("on", "moisture", "binary_sensor.a", "is detecting water"),
    ("on", "heat", "binary_sensor.a", "is detecting excessive heat"),
    ("on", "tamper", "binary_sensor.a", "has been tampered with"),
    ("on", "vibration", "binary_sensor.a", "is detecting vibration"),
    ("on", "sound", "binary_sensor.a", "is detecting sound"),
    ("on", "problem", "binary_sensor.a", "is reporting a problem"),
    ("on", "safety", "binary_sensor.a", "has raised an alert"),
    ("ON", "Smoke", "binary_sensor.a", "is detecting smoke"),
    ("on", "", "binary_sensor.sink_leak", "is detecting water"),
    ("on", "", "binary_sensor.basement_flood", "is detecting water"),
    ("on", "", "binary_sensor.water_heater", "is detecting water"),
    ("on", "door", "binary_sensor.a", "is open"),
    ("on", "", "binary_sensor.side_gate", "is open"),
    ("on", "", "binary_sensor.garage_tilt", "is open"),
    ("on", "motion", "binary_sensor.a", "has detected motion"),
    ("on", "presence", "binary_sensor.a", "has detected motion"),
    ("on", "", "binary_sensor.hall_occupancy", "has detected motion"),
    ("on", "", "switch.kettle", "is on"),
    ("off", "smoke", "binary_sensor.a", "has cleared"),
    ("off", "", "binary_sensor.sink_leak", "has cleared"),
    ("off", "window", "binary_sensor.a", "is closed"),
    ("off", "", "binary_sensor.hall_motion", "has gone quiet"),
    ("off", "", "switch.kettle", "is off"),
])
def test_state_phrase_binary(lm, to, dc, eid, phrase):
    assert lm._state_phrase(to, dc, eid) == phrase


def test_leak_outranks_opening(lm):
    assert lm._state_phrase("on", "", "binary_sensor.water_window") == "is detecting water"
    assert lm._state_phrase("on", "door", "binary_sensor.water_door") == "is detecting water"
    assert lm._state_phrase("on", "", "binary_sensor.leak_window") == "is detecting water"
    assert lm._state_phrase("off", "", "binary_sensor.leak_window") == "has cleared"


@pytest.mark.parametrize("to,dc,eid,phrase", [
    ("5", "battery", "sensor.remote", "battery is at 5%"),
    ("5.0", "", "sensor.remote_battery", "battery is at 5%"),
    ("5.5", "battery", "sensor.remote", "battery is at 5.5%"),
    ("21.5", "temperature", "sensor.lounge_temperature", "reads 21.5"),
    (" 21 ", "", "sensor.lounge", "reads  21 "),
    ("7", "", "number.volume", "is now 7"),
])
def test_state_phrase_numeric(lm, to, dc, eid, phrase):
    assert lm._state_phrase(to, dc, eid) == phrase


@pytest.mark.parametrize("to,phrase", [
    ("open", "is open"), ("closed", "is closed"), ("opening", "is opening"),
    ("closing", "is closing"), ("unlocked", "is unlocked"), ("locked", "is locked"),
    ("detected", "has detected activity"), ("home", "has arrived home"),
    ("not_home", "has left"), ("unavailable", "has gone unresponsive"),
    ("unknown", "is in an unknown state"), ("Open", "is open"),
    ("idle", "is now idle"), ("", "needs attention"), (None, "needs attention"),
])
def test_state_phrase_table(lm, to, phrase):
    assert lm._state_phrase(to, "", "cover.x") == phrase


def test_state_phrase_defaults(lm):
    assert lm._state_phrase("closed") == "is closed"


# ── _pick ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("eid,hour", [(DOOR, 14), ("sensor.x", 3), ("", 0), (DOOR, 15)])
def test_pick_is_md5_stable(lm, eid, hour):
    variants = ["a", "b", "c", "d", "e"]
    idx = int(hashlib.md5(f"{eid}|{hour}".encode()).hexdigest()[:6], 16) % 5
    assert lm._pick(variants, eid, hour) == variants[idx]
    assert lm._pick(variants, eid, hour) == lm._pick(variants, eid, hour)


# ── composing speech ───────────────────────────────────────────────────────

NOVEL = ["the first time I've observed this", "I haven't seen this before",
         "a first in my records"]


def test_compose_plain(lm):
    out = lm._compose("sir", "Kitchen Door", DOOR, "on", 14, novelty="unknown",
                      away=False, escalated=False, device_class="door")
    assert out == "Sir, Kitchen Door is open."


def test_compose_escalated_uses_urgent_register(lm):
    out = lm._compose("sir", "Kitchen Door", DOOR, "on", 14, novelty="unknown",
                      away=True, escalated=True, device_class="door")
    assert out == "Sir, your attention — Kitchen Door is open — while no one is home."


def test_compose_urgency_overrides_escalation(lm):
    grave = lm._compose("sir", "Hall Smoke", "binary_sensor.hall_smoke", "on", 14,
                        novelty="unknown", away=False, escalated=False,
                        device_class="smoke", urgency="critical")
    assert grave == "Sir. Hall Smoke is detecting smoke."
    neutral = lm._compose("sir", "Hall Smoke", "binary_sensor.hall_smoke", "on", 14,
                          novelty="unknown", away=False, escalated=True,
                          device_class="smoke", urgency="medium")
    assert neutral.startswith("Sir, Hall Smoke")
    high = lm._compose("sir", "X", "switch.x", "on", 14, novelty="", away=False,
                       escalated=False, urgency="high")
    assert high == "Sir, your attention — X is on."


def test_compose_novel_clause(lm):
    out = lm._compose("sir", "Kitchen Door", DOOR, "on", 14, novelty="novel",
                      away=True, escalated=False, device_class="door")
    clause = lm._pick(NOVEL, DOOR, 14)
    assert out == f"Sir, Kitchen Door is open — while no one is home, {clause}."


@pytest.mark.parametrize("hour,clause", [
    (0, "which is unusual at this hour of the night"),
    (5, "which is unusual at this hour of the night"),
    (6, "which is out of pattern for this time of day"),
    (23, "which is out of pattern for this time of day"),
])
def test_compose_unusual_hour_clause(lm, hour, clause):
    out = lm._compose("sir", "Kitchen Door", DOOR, "on", hour, novelty="unusual_hour",
                      away=False, escalated=False, device_class="door")
    assert out == f"Sir, Kitchen Door is open — {clause}."


def test_compose_name_fallbacks(lm):
    assert lm._compose("sir", "", "binary_sensor.back_garden_gate", "on", 1,
                       novelty="", away=False, escalated=False) == \
        "Sir, Back Garden Gate is open."
    assert lm._compose("sir", "", "", "on", 1, novelty="", away=False,
                       escalated=False) == "Sir, A device is on."


def test_compose_without_honorific(lm):
    assert lm._compose("", "Kitchen Door", DOOR, "on", 1, novelty="", away=False,
                       escalated=False) == "For your awareness — Kitchen Door is open."


def test_compose_persona_failure_falls_back(lm, load, monkeypatch):
    persona = load("persona")

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(persona, "announce_opener", boom)
    assert lm._compose("ma'am", "Kitchen Door", DOOR, "on", 1, novelty="",
                       away=False, escalated=False) == "Ma'am, Kitchen Door is open."
    assert lm._compose("", "Kitchen Door", DOOR, "on", 1, novelty="",
                       away=False, escalated=False) == "Kitchen Door is open."


def test_compose_announcement_passes_through(lm):
    out = lm.compose_announcement("sir", "Kitchen Door", DOOR, "on", "door", hour=3,
                                  novelty="unusual_hour", away=True)
    assert out == ("Sir, Kitchen Door is open — while no one is home, "
                   "which is unusual at this hour of the night.")


def test_compose_announcement_defaults(lm):
    assert lm.compose_announcement("sir", "Kettle") == "Sir, Kettle needs attention."
    assert lm.compose_announcement("sir", "Kettle", "", "on", escalated=True) == \
        "Sir, your attention — Kettle is on."
    assert lm.compose_announcement("sir", "Kettle", "", "on", urgency="critical") == \
        "Sir. Kettle is on."


# ── assess_core ────────────────────────────────────────────────────────────

def _core(lm, **over):
    kw = dict(honorific="sir", entity_id=DOOR, domain="binary_sensor", device_class="door",
              category="doors_windows", from_state="off", to_state="on",
              friendly_name="Kitchen Door", urgency="medium", anyone_home=True,
              recent_announcements=[], hour=14, history={"grade": "unknown"},
              prior=(0, 0))
    kw.update(over)
    return lm.assess_core(**kw)


def test_core_critical_always_speaks(lm):
    out = _core(lm, urgency="critical", recent_announcements=["kitchen door"])
    assert out["speak"] is True and out["urgency"] == "critical"
    assert out["reason"] == "local mind: critical urgency — always voiced"
    assert out["message"] == "Sir, your attention — Kitchen Door is open."


def test_core_duplicate_is_silent(lm):
    out = _core(lm, recent_announcements=["Sir, Kitchen Door is open."])
    assert out == {"speak": False, "urgency": "medium",
                   "reason": "local mind: already announced this device recently"}


def test_core_flapping_is_silent(lm):
    _core(lm, urgency="low")
    _core(lm, urgency="low")
    out = _core(lm, urgency="high")
    assert out["speak"] is False
    assert out["reason"] == ("local mind: Kitchen Door is flapping (≥3 events in 5m) "
                             "— suppressed")


def test_core_flap_key_falls_back_to_name(lm):
    for _ in range(2):
        _core(lm, entity_id="", friendly_name="Mystery", urgency="low")
    assert "Mystery" in lm._recent_events
    for _ in range(2):
        _core(lm, entity_id="", friendly_name="", urgency="low")
    assert "?" in lm._recent_events


def test_core_entry_point_while_away_escalates(lm):
    out = _core(lm, anyone_home=False)
    assert out["speak"] is True and out["urgency"] == "high"
    assert out["reason"] == "local mind: entry point active while away (unknown) — escalated"
    assert out["message"] == ("Sir, your attention — Kitchen Door is open — "
                              "while no one is home.")


def test_core_high_speaks(lm):
    out = _core(lm, urgency="high", history={"grade": "routine"})
    assert out["speak"] and out["urgency"] == "high"
    assert out["reason"] == "local mind: high urgency (routine)"


@pytest.mark.parametrize("prior,speak,reason", [
    ((0, 2), False, "local mind: 2 similar past events judged routine (case memory)"),
    ((3, 0), True, "local mind: 3 similar past events voiced (case memory)"),
])
def test_core_case_memory(lm, prior, speak, reason):
    out = _core(lm, prior=prior, history={"grade": "novel"})
    assert out["speak"] is speak and out["reason"] == reason


@pytest.mark.parametrize("history,speak,reason", [
    ({"grade": "novel"}, True, "local mind: novel event — never observed before"),
    ({"grade": "unusual_hour", "total": 4}, True,
     "local mind: out of hourly pattern (seen 4× overall, never near this hour)"),
    ({"grade": "routine", "at_hour": 9, "days": 12.0}, False,
     "local mind: routine at this hour (~9× in 12.0d)"),
    ({"grade": "common", "at_hour": 2, "days": 10.0}, False,
     "local mind: common at this hour (~2× in 10.0d)"),
    ({"grade": "occasional"}, False, "local mind: occasional while home — not worth voicing"),
    ({}, False, "local mind: unknown while home — not worth voicing"),
])
def test_core_medium_by_history(lm, history, speak, reason):
    out = _core(lm, entity_id="sensor.basement_humidity", domain="sensor",
                device_class="humidity", friendly_name="Basement Humidity", to_state="71",
                history=history, prior=(1, 1))
    assert out["speak"] is speak and out["reason"] == reason


def test_core_medium_unknown_while_away_is_surfaced(lm):
    out = _core(lm, entity_id="sensor.basement_humidity", domain="sensor",
                device_class="humidity", friendly_name="Basement Humidity",
                to_state="71", anyone_home=False)
    assert out["speak"] is True and out["urgency"] == "medium"
    assert out["reason"] == "local mind: unknown while away — surfaced"
    assert out["message"] == "Sir, Basement Humidity reads 71 — while no one is home."


def test_core_low_is_silent(lm):
    assert _core(lm, urgency="low") == {"speak": False, "urgency": "low",
                                        "reason": "local mind: low urgency — silent"}


def test_core_novel_message_has_the_clause(lm):
    out = _core(lm, entity_id="switch.shed_heater", domain="switch", device_class="",
                friendly_name="Shed Heater", history={"grade": "novel"})
    assert out["message"] == f"Sir, Shed Heater is on — {lm._pick(NOVEL, 'switch.shed_heater', 14)}."


def test_core_counts_decisions(lm):
    _core(lm, urgency="critical")
    _core(lm, urgency="low", entity_id="switch.a")
    _core(lm, urgency="low", entity_id="switch.b")
    assert lm.stats() == {"decisions": 3, "spoke": 1, "silent": 2}


def test_stats_is_a_copy(lm):
    s = lm.stats()
    s["decisions"] = 99
    assert lm.stats()["decisions"] == 0


# ── assess (async wrapper) ─────────────────────────────────────────────────

async def test_assess_uses_history_prior_and_logs(lm, load, fake_hass, monkeypatch):
    rc = load("reasoning_cache")
    monkeypatch.setattr(rc, "similar", lambda *a: (0, 2))
    logged = []
    _log_to(monkeypatch, lambda tag, text: logged.append((tag, text)))
    out = await lm.assess(fake_hass, honorific="sir", entity_id="sensor.basement_humidity",
                          domain="sensor", device_class="humidity", category="climate",
                          from_state="50", to_state="71", friendly_name="Basement Humidity",
                          urgency="medium", anyone_home=True)
    assert out["speak"] is False
    assert out["reason"] == "local mind: 2 similar past events judged routine (case memory)"
    assert logged == [("LOCAL", "sensor.basement_humidity: silent — "
                                "2 similar past events judged routine (case memory)")]


async def test_assess_speak_log_and_defaults(lm, load, fake_hass, monkeypatch):
    rc = load("reasoning_cache")
    monkeypatch.setattr(rc, "similar", lambda *a: (0, 0))
    logged = []
    _log_to(monkeypatch, lambda tag, text: logged.append(text))
    out = await lm.assess(fake_hass, honorific="sir", friendly_name="Kettle", to_state="on")
    # defaults: urgency medium, nobody home → unknown while away surfaced
    assert out["speak"] is True and out["urgency"] == "medium"
    assert logged == ["Kettle: SPEAK — unknown while away — surfaced"]


async def test_assess_history_failure_degrades(lm, load, monkeypatch):
    rc = load("reasoning_cache")
    monkeypatch.setattr(rc, "similar", lambda *a: (0, 0))

    class _Hass:
        async def async_add_executor_job(self, *a):
            raise RuntimeError("executor gone")
    out = await lm.assess(_Hass(), honorific="sir", entity_id="switch.kettle",
                          domain="switch", to_state="on", friendly_name="Kettle",
                          anyone_home=True)
    assert out["reason"] == "local mind: unknown while home — not worth voicing"


async def test_assess_looks_up_history_through_the_executor(lm, load, monkeypatch):
    rc = load("reasoning_cache")
    monkeypatch.setattr(rc, "similar", lambda *a: (0, 0))
    jobs = []

    class _Hass:
        async def async_add_executor_job(self, fn, *args):
            jobs.append((fn, args))
            return {"grade": "novel", "total": 0, "at_hour": 0, "days": 5.0}
    out = await lm.assess(_Hass(), honorific="sir", entity_id="switch.kettle",
                          domain="switch", to_state="on", friendly_name="Kettle",
                          anyone_home=True)
    assert out["reason"] == "local mind: novel event — never observed before"
    fn, args = jobs[0]
    assert fn is lm.history_profile
    assert args[:2] == ("switch.kettle", "on") and 0 <= args[2] <= 23


async def test_assess_log_failure_is_swallowed(lm, load, fake_hass, monkeypatch):
    rc = load("reasoning_cache")
    monkeypatch.setattr(rc, "similar", lambda *a: (0, 0))

    def boom(*a):
        raise RuntimeError("log down")
    _log_to(monkeypatch, boom)
    out = await lm.assess(fake_hass, honorific="sir", entity_id="switch.kettle",
                          to_state="on", urgency="low")
    assert out["speak"] is False
