"""Local cognition checked against its spec (7d).

The older cognition tests cover noise control, the evidence rule behind
predict, departures, routines, presence and restarts. These pin the rest:
the salience scores, the model's bookkeeping, the wording and shapes of the
anticipation actions, and the small helpers they rest on."""
from __future__ import annotations

import datetime
import json
import time
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def cog(load, monkeypatch):
    c = load("cognition")
    c.reset()
    c._DEPART_TRAVEL.clear()
    c._DEPART_STAGES.clear()
    monkeypatch.setattr(c, "_log_decision", lambda *a, **k: 77)
    yield c
    c.reset()


def _event(entity_id, value, dclass=None, old=None):
    new = types.SimpleNamespace(state=value, attributes={"device_class": dclass})
    before = types.SimpleNamespace(state=old) if old is not None else None
    return types.SimpleNamespace(data={"entity_id": entity_id, "new_state": new,
                                       "old_state": before})


def _at(year, month, day, hour, minute=0):
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


# ── salience ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain, dclass, value, expected", [
    ("binary_sensor", "smoke", "on", True), ("binary_sensor", "smoke", "ON", True),
    ("binary_sensor", "gas", "off", False), ("binary_sensor", "smoke", None, False),
    ("binary_sensor", "smoke", "unavailable", False),
    ("alarm_control_panel", None, "triggered", True),
    ("alarm_control_panel", None, "pending", True),
    ("alarm_control_panel", None, "armed_away", False),
    ("lock", None, "unlocked", False),
])
def test_safety_triggers(cog, domain, dclass, value, expected):
    assert cog._is_safety_trigger(domain, dclass, value) is expected


def _fresh(cog):
    return cog._Entry(time.time())


@pytest.mark.parametrize("domain, dclass, value, eid, score, reason", [
    ("binary_sensor", "smoke", "on", "", 0.9, "safety/security trigger"),
    ("binary_sensor", "problem", "on", "binary_sensor.backup_stale", 0.15,
     "system maintenance — informational"),
    ("binary_sensor", "problem", "on", "binary_sensor.pump", 0.4, "device problem reported"),
    ("binary_sensor", "problem", "off", "", 0.0, "routine"),
    ("cover", "blind", "open", "", 0.55, "access-point"),
    ("binary_sensor", "window", "on", "", 0.55, "access-point"),
    ("lock", None, "unlocked", "", 0.55, "access-point"),
    ("light", None, "on", "", 0.0, "routine"),
])
def test_base_salience(cog, domain, dclass, value, eid, score, reason):
    assert cog._salience(domain, dclass, value, _fresh(cog), time.time(), eid) == (score, reason)


def test_rare_and_unusual_hour_add_up(cog):
    now = _at(2026, 6, 1, 3)
    entry = cog._Entry(now - 10 * 86400)
    entry.transitions.extend([now - 86400, now - 3600])
    entry.hours[12] = 30
    score, reason = cog._salience("light", None, "on", entry, now)
    assert score == pytest.approx(0.25) and reason == "rare transition, unusual hour"
    score, _r = cog._salience("binary_sensor", "smoke", "on", entry, now)
    assert score == 1.0


def test_rare_needs_two_transitions(cog):
    entry = cog._Entry(0.0)
    entry.transitions.append(1.0)
    assert cog._is_rare(entry, 1e9) is False
    entry.transitions.append(2.0)
    assert cog._is_rare(entry, 2.0 * 86400) is True


def test_an_unusual_hour_needs_twenty_changes(cog):
    entry = cog._Entry(0.0)
    entry.hours[12] = 19
    assert cog._is_unusual_hour(entry, _at(2026, 6, 1, 3)) is False
    entry.hours[3] = 1
    assert cog._is_unusual_hour(entry, _at(2026, 6, 1, 3)) is False   # 5%
    entry.hours[12] = 60
    assert cog._is_unusual_hour(entry, _at(2026, 6, 1, 3)) is True    # under 2%


# ── process and the model ───────────────────────────────────────────────────

def test_process_counts_and_starts_observing(cog, monkeypatch):
    monkeypatch.setattr(cog.time, "time", lambda: 5000.0)
    d = cog.process(_event("light.hall", "on"))
    assert d == cog.Decision(escalate=False, salience=0.0, reason="routine")
    assert cog._OBSERVING_SINCE == 5000.0
    cog.process(_event("binary_sensor.smoke", "on", "smoke"))
    assert cog.stats()["events_seen"] == 2 and cog.stats()["anomalies_escalated"] == 1


def test_a_recovery_is_neither_escalated_nor_learned(cog):
    cog.process(_event("binary_sensor.front", "off", "door", old="on"))
    d = cog.process(_event("binary_sensor.front", "on", "door", old="unavailable"))
    assert d == cog.Decision(False, 0.0, "state recovery")
    entry = cog._MODEL["binary_sensor.front"]
    assert entry.last_state == "on" and len(entry.transitions) == 1


def test_a_broken_event_is_an_error_decision(cog):
    assert cog.process(types.SimpleNamespace(data=None)) == cog.Decision(False, 0.0, "error")


def test_observe_counts_only_real_changes(cog):
    now = _at(2026, 6, 1, 9)
    entry = cog.observe("light.a", None, "on", now)
    cog.observe("light.a", None, "on", now + 5)
    cog.observe("light.a", None, None, now + 10)
    assert list(entry.transitions) == [now, now + 10] and entry.hours[9] == 2
    assert entry.last_state == "on" and entry.last_changed == now + 10
    assert list(entry.daily_first) == [(cog._local_day(now), 9 * 3600)]


def test_the_model_evicts_the_stalest(cog, monkeypatch):
    monkeypatch.setattr(cog, "MODEL_MAX_ENTITIES", 2)
    cog.observe("a.x", None, "1", 10.0)
    cog.observe("b.x", None, "1", 5.0)
    cog.observe("c.x", None, "1", 20.0)
    assert set(cog._MODEL) == {"a.x", "c.x"}


def test_stats_counts_learning(cog, load):
    entry = cog._Entry(0.0)
    entry.occ = {8: {"locked": 20}}
    cog._MODEL["lock.front"] = entry
    cog._MODEL["light.x"] = cog._Entry(0.0)
    st = cog.stats()
    assert st == {"entities_tracked": 2, "events_seen": 0, "anomalies_escalated": 0,
                  "predictable": 1, "routines": 0, "presence_routines": 0}


def test_reset_and_mark_unobserved(cog):
    cog.process(_event("light.a", "on"))
    cog._RECUR_ALERTED["k"] = 1
    cog.mark_unobserved()
    assert cog._OBSERVING_SINCE == 0.0 and cog._MODEL
    cog.reset()
    assert not cog._MODEL and not cog._RECUR_ALERTED and cog.stats()["events_seen"] == 0


# ── occupancy ───────────────────────────────────────────────────────────────

def test_occupancy_samples_only_the_right_entities(cog):
    hass = FakeHass()
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("cover.garage", "closed")
    hass.states.async_set("binary_sensor.back", "off", {"device_class": "door"})
    hass.states.async_set("binary_sensor.motion", "on", {"device_class": "motion"})
    hass.states.async_set("alarm_control_panel.home", "unavailable")
    now = _at(2026, 6, 1, 14)
    assert cog.sample_occupancy(hass, now) == 3
    assert cog._MODEL["lock.front"].occ == {14: {"locked": 1}}
    assert cog._MODEL["lock.front"].last_state == "locked"
    assert "binary_sensor.motion" not in cog._MODEL


def test_occupancy_counts_halve_past_the_guard(cog):
    hass = FakeHass()
    hass.states.async_set("lock.front", "locked")
    now = _at(2026, 6, 1, 14)
    entry = cog._Entry(now)
    entry.occ = {14: {"locked": 50000, "unlocked": 9}}
    cog._MODEL["lock.front"] = entry
    cog.sample_occupancy(hass, now)
    assert entry.occ[14] == {"locked": 25000, "unlocked": 4}


@pytest.mark.parametrize("state, dclass, said", [
    ("on", "door", "open"), ("off", "garage", "closed"), ("on", None, "on"),
    ("armed_away", None, "armed away"), ("jammed", "window", "jammed"),
])
def test_humanised_states(cog, state, dclass, said):
    assert cog._humanize(state, dclass) == said


def test_a_flagged_deviation_has_every_field(cog, monkeypatch):
    hass = FakeHass()
    hass.states.async_set("lock.back", "unlocked", {"friendly_name": "Back Lock"})
    now = _at(2026, 6, 1, 14)
    entry = cog._Entry(now - 3600)
    entry.occ = {14: {"locked": 30}}
    cog._MODEL["lock.back"] = entry
    monkeypatch.setattr(cog, "_all_tracked_residents_away", lambda hass: False)
    monkeypatch.setattr(cog, "_conflicting_armed_alarm", lambda hass: True)
    out = cog.predict(hass, now)
    assert out == [{"type": "anticipation", "urgency": "medium",
                    "message": "Back Lock is unlocked while the alarm is armed.",
                    "pattern_key": "anticipate:lock.back", "entity_id": "lock.back",
                    "offer": False}]
    assert cog.predict(hass, now + 60) == []          # hourly cooldown


def test_a_binary_sensor_deviation_is_low(cog, monkeypatch):
    hass = FakeHass()
    hass.states.async_set("binary_sensor.w", "on", {"device_class": "window"})
    now = _at(2026, 6, 1, 14)
    entry = cog._Entry(now - 3600)
    entry.occ = {14: {"off": 30}}
    cog._MODEL["binary_sensor.w"] = entry
    monkeypatch.setattr(cog, "_all_tracked_residents_away", lambda hass: True)
    (item,) = cog.predict(hass, now)
    assert item["urgency"] == "low"
    assert item["message"] == "binary_sensor.w is open while nobody appears to be home."


# ── routines ────────────────────────────────────────────────────────────────

def test_a_routine_needs_days_spread_and_regularity(cog, monkeypatch):
    monkeypatch.setattr(cog, "_adaptive_extra_days", lambda: 0)
    pts = [(d, 8 * 3600) for d in range(7)]
    assert cog._routine_of_pts(pts) == (8 * 3600, 0.0)
    assert cog._routine_of_pts(pts[:6]) is None
    assert cog._routine_of_pts([(d * 2, 8 * 3600) for d in range(7)]) is None
    spread = [(d, 8 * 3600 + (d % 2) * 6000) for d in range(8)]
    assert cog._routine_of_pts(spread) is None
    monkeypatch.setattr(cog, "_adaptive_extra_days", lambda: 1)
    assert cog._routine_of_pts(pts) is None


@pytest.mark.parametrize("secs, text", [(0, "00:00"), (3661, "01:01"), (86399, "23:59")])
def test_hhmm(cog, secs, text):
    assert cog._hhmm(secs) == text


def test_overdue_wording_and_once_a_day(cog, monkeypatch):
    hass = FakeHass()
    hass.states.async_set("binary_sensor.door", "off", {"friendly_name": "Front Door"})
    now = _at(2026, 6, 10, 10)
    today = cog._local_day(now)
    entry = cog._Entry(now - 20 * 86400)
    entry.daily_first.extend((today - d, 7 * 3600 + 45 * 60) for d in range(1, 9))
    entry.last_first_day = today - 1
    cog._MODEL["binary_sensor.door"] = entry
    monkeypatch.setattr(cog, "_adaptive_extra_days", lambda: 0)
    monkeypatch.setattr(cog, "_adaptive_tol_scale", lambda: 1.0)
    cog._OBSERVING_SINCE = now - 86400
    out = cog.predict_overdue(hass, now)
    assert out == [{"type": "anticipation_overdue", "urgency": "low",
                    "message": "Front Door usually has activity by around 07:45, but there's "
                               "been none yet today — thought it worth flagging.",
                    "pattern_key": "overdue:binary_sensor.door", "offer": False,
                    "decision_id": 77}]
    assert cog.predict_overdue(hass, now) == []


# ── presence helpers ────────────────────────────────────────────────────────

def test_home_and_away(cog):
    assert cog._is_home("home") and not cog._is_home("Home")
    assert cog._is_away("work") and not cog._is_away("unknown") and not cog._is_away(None)


def test_debounce_by_tracker_kind(cog):
    gps = types.SimpleNamespace(attributes={"source_type": "GPS"})
    coords = types.SimpleNamespace(attributes={"latitude": 1, "longitude": 2})
    router = types.SimpleNamespace(attributes={"source_type": "router"})
    assert cog._debounce_for(gps) == 60 and cog._debounce_for(coords) == 60
    assert cog._debounce_for(router) == 300


def test_coordinates_and_distance(cog):
    hass = FakeHass()
    hass.config = types.SimpleNamespace(latitude=0.0, longitude=0.0)
    bad = types.SimpleNamespace(attributes={"latitude": "x", "longitude": 1})
    assert cog._entity_coords(bad) is None
    assert cog._home_coords(hass) == (0.0, 0.0)
    hass.states.async_set("zone.home", "zoning", {"latitude": 0.0, "longitude": 1.0})
    st = types.SimpleNamespace(attributes={"latitude": 0.0, "longitude": 0.0})
    assert cog.distance_home_km(hass, st) == pytest.approx(111.19, abs=0.01)
    assert cog.distance_home_km(hass, types.SimpleNamespace(attributes={})) is None


def test_routine_presence_prefers_people(cog):
    hass = FakeHass()
    hass.states.async_set("device_tracker.phone", "home")
    assert cog._routine_presence_domains(hass) == {"device_tracker"}
    hass.states.async_set("person.a", "home")
    assert cog._routine_presence_domains(hass) == {"person"}
    assert [s.entity_id for s in cog._routine_presence_entities(hass)] == ["person.a"]
    assert cog._people_home(hass) == {"person.a"}


def test_presence_status_rows(cog):
    hass = FakeHass()
    hass.config = types.SimpleNamespace(latitude=0.0, longitude=0.0)
    hass.states.async_set("person.a", "home", {"friendly_name": "A"})
    hass.states.async_set("person.b", "not_home", {"latitude": 0.0, "longitude": 0.1})
    hass.states.async_set("person.c", "Work", {"source_type": "gps"})
    hass.states.async_set("person.d", "unknown")
    assert cog.presence_status(hass) == [
        {"entity_id": "person.a", "name": "A", "zone": "Home", "gps": False, "distance_km": None},
        {"entity_id": "person.b", "name": "person.b", "zone": "Away", "gps": True,
         "distance_km": 11.1},
        {"entity_id": "person.c", "name": "person.c", "zone": "Work", "gps": True,
         "distance_km": None},
    ]


def test_heading_home_wording(cog):
    hass = FakeHass()
    hass.config = types.SimpleNamespace(latitude=0.0, longitude=0.0)
    hass.states.async_set("person.a", "away", {"latitude": 0.0, "longitude": 0.05,
                                              "friendly_name": "A"})
    assert cog.predict_proximity(hass) == []
    hass.states.async_set("person.a", "away", {"latitude": 0.0, "longitude": 0.03,
                                              "friendly_name": "A"})
    assert cog.predict_proximity(hass) == [{
        "type": "anticipation_arriving", "urgency": "low",
        "message": "A is heading home — about 3.3 km out.",
        "pattern_key": "arriving:person.a", "offer": False}]
    hass.states.async_set("person.a", "home")
    cog.predict_proximity(hass)
    assert not cog._LAST_DIST and not cog._APPROACH_ALERTED


# ── departures: pure parts ──────────────────────────────────────────────────

@pytest.mark.parametrize("location, real", [
    ("12 High Street", True), ("The Library", True), (None, False), ("  ", False),
    ("Home", False), ("my  house", False), ("https://x.example", False),
    ("Zoom call", False), ("TBC", False), ("+353 1 234 5678", False),
    ("Room 101234 B", True), ("Cosy Cottage", False),
])
def test_real_places(cog, location, real):
    assert cog.is_real_place(location, ("Cosy Cottage",)) is real


def test_home_names(cog):
    hass = FakeHass()
    hass.config = types.SimpleNamespace(location_name="Base")
    hass.states.async_set("zone.home", "zoning", {"friendly_name": "  "})
    assert cog._home_names(hass) == ("Base",)


@pytest.mark.parametrize("raw, expected", [
    (None, set()), ("", set()), ("bad", set()), ('["calendar.a"]', {"calendar.a"}),
    (["calendar.b", 3], {"calendar.b", "3"}), ({"calendar.c"}, {"calendar.c"}), (5, set()),
])
def test_excluded_calendars(cog, raw, expected):
    cfg = types.SimpleNamespace(get=lambda k, d=None: d if raw is None else raw)
    assert cog._excluded_calendars(cfg) == expected


@pytest.mark.parametrize("reason, wait", [
    ("Google found no transit route", 999), ("the Routes API is not enabled", 1800),
    ("timeout", 300), (None, 300),
])
def test_retry_after(cog, reason, wait):
    assert cog._retry_after(reason, 999) == wait


async def test_cached_lookups(cog):
    calls = []

    async def fetch():
        calls.append(1)
        return (None, None) if len(calls) == 1 else (12.0, None)
    assert await cog._cached_lookup("k", 1000.0, 600, fetch, "x") == (None, "no travel time came back")
    assert await cog._cached_lookup("k", 1100.0, 600, fetch, "x") == (None, "no travel time came back")
    assert await cog._cached_lookup("k", 1400.0, 600, fetch, "x") == (12.0, None)
    assert await cog._cached_lookup("k", 1900.0, 600, fetch, "x") == (12.0, None)
    assert len(calls) == 2
    cog._DEPART_TRAVEL["old"] = (1.0, None, 0.0)
    await cog._cached_lookup("k", 1000.0 + 6 * 3600 + 61, 600, fetch, "x")
    assert "old" not in cog._DEPART_TRAVEL


def test_modes_enabled(cog):
    cfg = types.SimpleNamespace(get=lambda k, d=None: k != "departure_mode_transit")
    assert cog._modes_enabled(cfg) == ["walk", "drive"]


def test_leave_groups_merge_close_modes(cog):
    start = datetime.datetime(2026, 6, 1, 12, 0)
    groups = cog._leave_groups({"walk": 40.0, "transit": 20.0, "drive": 18.0}, start, 30)
    assert groups == [
        {"modes": ["walk"], "leave_at": datetime.datetime(2026, 6, 1, 11, 15), "lead": 45.0},
        {"modes": ["transit", "drive"], "leave_at": datetime.datetime(2026, 6, 1, 11, 35),
         "lead": 25.0},
    ]
    (only,) = cog._leave_groups({"any": None}, start, 30)
    assert only == {"modes": ["any"], "leave_at": datetime.datetime(2026, 6, 1, 11, 30),
                    "lead": 30}


def test_departure_messages(cog):
    now = datetime.datetime(2026, 6, 1, 11, 15)
    one = [{"modes": ["drive"], "leave_at": now}]
    assert cog._departure_message("Lunch", " at Cafe", 45, one, now, 30, None) == (
        "Heads up — Lunch at Cafe begins in about 45 minutes; you'll want to head out.")
    assert cog._departure_message("Lunch", "", 45, [{"modes": ["any"], "leave_at": now}],
                                  now, 30, "no route") == (
        "Heads up — Lunch begins in about 45 minutes; you'll want to head out. I couldn't "
        "work out the travel time, so I used the usual 30 minutes.")
    many = [{"modes": ["walk"], "leave_at": now},
            {"modes": ["transit", "drive"], "leave_at": now + datetime.timedelta(minutes=20)},
            {"modes": ["any"], "leave_at": now + datetime.timedelta(minutes=25)}]
    assert cog._departure_message("Lunch", "", 45, many, now, 30, None) == (
        "Heads up — Lunch begins in about 45 minutes. Head out now if you're walking, "
        "leave in 20 minutes if you're taking public transport or driving, or leave in 25 "
        "minutes if you're heading out.")


async def test_a_later_leave_time_reminds_only_if_everyone_stayed(cog):
    hass = FakeHass()
    hass.states.async_set("person.a", "home")
    now_dt = datetime.datetime(2026, 6, 1, 11, 40)
    start = datetime.datetime(2026, 6, 1, 12, 0)
    cog._DEPART_STAGES["depart:x"] = {
        "title": "Lunch", "start": start, "home": {"person.a"}, "done": set(),
        "groups": [{"modes": ["walk"], "leave_at": start},
                   {"modes": ["drive"], "leave_at": datetime.datetime(2026, 6, 1, 11, 35)}]}
    out = cog._departure_followups(hass, 0, now_dt, 5)
    assert out == [{"type": "anticipation_departure", "urgency": "low",
                    "message": "Lunch begins in about 20 minutes and everyone is still home. "
                               "If you're driving, it's time to leave.",
                    "pattern_key": "depart:x:1", "offer": False, "decision_id": 77}]
    assert cog._departure_followups(hass, 0, now_dt, 5) == []
    assert cog._departure_followups(hass, 0, start, 5) == []
    assert "depart:x" not in cog._DEPART_STAGES


def test_person_names(cog, load, monkeypatch):
    hass = FakeHass()
    hass.states.async_set("person.sam_x", "home", {"friendly_name": "Sam X"})
    assert cog._person_name(hass, "sam_x") == "Sam X"
    assert cog._person_name(hass, "jo_ann") == "Jo Ann"


# ── persistence ─────────────────────────────────────────────────────────────

def test_an_entry_survives_a_round_trip(cog):
    e = cog._Entry(100.0)
    e.last_state, e.last_changed = "on", 150.0
    e.transitions.extend([110.0, 150.0])
    e.hours[3] = 2
    e.occ = {3: {"on": 4}}
    e.daily_first.append((10, 60))
    e.depart_first.append((10, 70))
    e.return_first.append((10, 80))
    e.pres_depart_day, e.pres_return_day, e.last_depart_ts = 10, 10, 140.0
    e.home_since_depart = False
    saved = json.loads(json.dumps(cog._entry_to_dict(e)))
    assert saved["occ"] == {"3": {"on": 4}}
    back = cog._entry_from_dict(saved)
    assert cog._entry_to_dict(back) == cog._entry_to_dict(e)
    assert back.transitions.maxlen == cog.TRANSITION_WINDOW and back.depart_first.maxlen == 240


def test_an_entry_with_bad_hours_starts_clean(cog):
    e = cog._entry_from_dict({"first_seen": 5.0, "hours": [1, 2]})
    assert e.hours == [0] * 24 and e.last_changed == 5.0 and e.home_since_depart is True
    assert e.last_depart_ts == 0.0


def test_the_ledger_keeps_two_days_and_the_newest(cog, monkeypatch):
    monkeypatch.setattr(cog, "RECUR_ALERT_MAX", 2)
    cog._RECUR_ALERTED.update({"a": 10, "b": 9, "c": 8, "d": 10, 5: 10, "e": "10"})
    kept = cog._prune_alerted(10)
    assert kept == [("a", 10), ("d", 10)]
    assert cog._RECUR_ALERTED == {"a": 10, "d": 10}


def test_save_and_load_round_trip(cog, tmp_path):
    db = str(tmp_path / "patterns.db")
    now = _at(2026, 6, 1, 9)
    cog.observe("light.a", None, "on", now)
    cog._RECUR_ALERTED["k"] = cog._local_day(now)
    assert cog.save_to_db(db, now) == 1
    cog.reset()
    assert cog.load_from_db(db, now) == 1
    assert cog._MODEL["light.a"].last_state == "on"
    assert cog._RECUR_ALERTED == {"k": cog._local_day(now)}


def test_a_failed_save_is_zero(cog, tmp_path):
    assert cog.save_to_db(str(tmp_path / "missing" / "x.db")) == 0
