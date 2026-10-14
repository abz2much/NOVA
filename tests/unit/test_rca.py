"""Fresh behaviour tests for rca.py (root cause analysis), from the spec.

Each test builds small patterns/conversations databases with the columns the
state logger and activity log write, then asks why something changed."""
import datetime as dt
import logging
import os
import sqlite3

import pytest

EVENT = "2026-03-14 21:00:00"


def ago(seconds, base=EVENT):
    """A space-form timestamp `seconds` before the focal moment."""
    return (dt.datetime.fromisoformat(base) - dt.timedelta(seconds=seconds)).isoformat(sep=" ")


@pytest.fixture
def rca(load):
    return load("rca")


class Home:
    def __init__(self, root):
        self.patterns = str(root / "patterns.db")
        self.activity = str(root / "conversations.db")
        with sqlite3.connect(self.patterns) as c:
            c.execute("CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "timestamp TEXT NOT NULL, entity_id TEXT NOT NULL, domain TEXT NOT NULL, "
                      "old_state TEXT, new_state TEXT NOT NULL, area_id TEXT, hour INTEGER, "
                      "day_of_week INTEGER, triggered_by TEXT DEFAULT 'system')")
            c.execute("CREATE TABLE commands (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "timestamp TEXT NOT NULL, text TEXT NOT NULL, handled_by TEXT DEFAULT 'agent', "
                      "entity_ids TEXT DEFAULT '[]', person TEXT DEFAULT 'unknown', hour INTEGER, "
                      "day_of_week INTEGER)")
        with sqlite3.connect(self.activity) as c:
            c.execute("CREATE TABLE activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "timestamp TEXT NOT NULL, entity_id TEXT NOT NULL DEFAULT '', "
                      "category TEXT NOT NULL DEFAULT 'other', urgency TEXT NOT NULL DEFAULT 'low', "
                      "message TEXT NOT NULL DEFAULT '', was_spoken INTEGER DEFAULT 0)")

    def change(self, ts, eid, old, new, area=None, hour=None, by="system"):
        with sqlite3.connect(self.patterns) as c:
            c.execute("INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
                      "new_state, area_id, hour, day_of_week, triggered_by) VALUES (?,?,?,?,?,?,?,?,?)",
                      (ts, eid, eid.split(".")[0], old, new, area, hour, 5, by))

    def said(self, ts, text, person=None):
        with sqlite3.connect(self.patterns) as c:
            if person is None:
                c.execute("INSERT INTO commands (timestamp, text) VALUES (?, ?)", (ts, text))
            else:
                c.execute("INSERT INTO commands (timestamp, text, person) VALUES (?,?,?)",
                          (ts, text, person))

    def did(self, ts, message, eid="", category=None):
        with sqlite3.connect(self.activity) as c:
            if category is None:
                c.execute("INSERT INTO activity_log (timestamp, entity_id, message) VALUES (?,?,?)",
                          (ts, eid, message))
            else:
                c.execute("INSERT INTO activity_log (timestamp, entity_id, category, message) "
                          "VALUES (?,?,?,?)", (ts, eid, category, message))

    def why(self, rca, eid, *args, **kw):
        return rca.analyze(eid, *args, patterns_db=self.patterns, activity_db=self.activity, **kw)


@pytest.fixture
def home(tmp_path):
    return Home(tmp_path)


def kinds(res):
    return [c["kind"] for c in res["candidates"]]


# ── interface ────────────────────────────────────────────────────────────────

def test_constants(rca):
    assert (rca.DEFAULT_WINDOW_SECS, rca.AFTER_WINDOW_SECS, rca.CASCADE_WINDOW_SECS,
            rca.NEAR_WINDOW_SECS, rca.SCHEDULE_LOOKBACK_DAYS, rca.SCHEDULE_MIN_HITS,
            rca.TIMELINE_CAP, rca.CANDIDATE_CAP) == (1800, 120, 90, 300, 14, 4, 40, 5)
    assert rca.PATTERNS_DB is None and rca.ACTIVITY_DB is None


def test_db_paths_resolve_through_paths_and_overrides(rca, load, monkeypatch, tmp_path):
    paths = load("paths")
    monkeypatch.setattr(paths, "_root", str(tmp_path))
    assert rca._patterns_db() == os.path.join(str(tmp_path), "nova", "patterns.db")
    assert rca._activity_db() == os.path.join(str(tmp_path), "nova", "conversations.db")
    monkeypatch.setattr(rca, "PATTERNS_DB", "/elsewhere/p.db")
    monkeypatch.setattr(rca, "ACTIVITY_DB", "/elsewhere/a.db")
    assert (rca._patterns_db(), rca._activity_db()) == ("/elsewhere/p.db", "/elsewhere/a.db")


def test_overrides_are_used_when_no_db_given(rca, home, monkeypatch):
    home.change(EVENT, "light.porch", "on", "off", by="automation.sunrise")
    monkeypatch.setattr(rca, "PATTERNS_DB", home.patterns)
    monkeypatch.setattr(rca, "ACTIVITY_DB", home.activity)
    assert rca.analyze("light.porch")["candidates"][0]["kind"] == "attributed"


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("2026-03-14 21:00:00", dt.datetime(2026, 3, 14, 21, 0)),
    ("2026-03-14T21:00:00", dt.datetime(2026, 3, 14, 21, 0)),
    (" 2026-03-14T21:00:00Z ", dt.datetime(2026, 3, 14, 21, 0)),
    ("2026-03-14T21:00:00+02:00", dt.datetime(2026, 3, 14, 21, 0)),   # offset dropped
    ("", None), (None, None), ("yesterday", None),
])
def test_dt_parses_and_strips_timezone(rca, raw, expected):
    got = rca._dt(raw)
    assert got == expected
    assert got is None or got.tzinfo is None


def test_secs_before(rca):
    t0 = dt.datetime(2026, 3, 14, 21, 0)
    assert rca._secs_before(t0, "2026-03-14 20:59:15") == 45.0
    assert rca._secs_before(t0, "2026-03-14 21:00:00") == 0.0
    assert rca._secs_before(t0, "2026-03-14 21:00:01") is None
    assert rca._secs_before(t0, "garbage") is None


@pytest.mark.parametrize("eid, toks", [
    ("binary_sensor.garage_hub_motion", {"garage", "hub", "motion"}),
    ("sensor.kitchen_sensor", {"kitchen"}),
    ("light.main_status_on", set()),
    ("switch.tv-power.2", {"power"}),
    ("climate.Den_Heat_Mode", {"den", "heat"}),
    ("nodot_bedroom_fan", {"nodot", "bedroom"}),
    (None, set()),
])
def test_tokens(rca, eid, toks):
    assert rca._tokens(eid) == toks


def test_rows_tolerates_missing_things(rca, home, tmp_path):
    assert rca._rows(str(tmp_path / "no" / "dir.db"), "SELECT 1", ()) == []
    assert rca._rows(home.patterns, "SELECT * FROM no_such_table", ()) == []
    assert rca._rows(home.patterns, "NOT SQL", ()) == []
    home.change(EVENT, "light.a", "off", "on")
    rows = rca._rows(home.patterns, "SELECT entity_id, new_state FROM state_changes", ())
    assert rows == [{"entity_id": "light.a", "new_state": "on"}]


def test_entity_names_from_states(rca, fake_hass):
    fake_hass.states.set("light.hall_lamp", "on", friendly_name=" Hall Lamp ")
    fake_hass.states.set("sensor.unnamed", "3")
    assert rca.entity_names(fake_hass) == {"light.hall_lamp": "Hall Lamp"}


def test_entity_names_failure_is_empty(rca):
    class Broken:
        @property
        def states(self):
            raise RuntimeError("no states")
    assert rca.entity_names(Broken()) == {}


# ── gather ───────────────────────────────────────────────────────────────────

def test_gather_without_history(rca, home):
    ev = rca.gather("light.nothing", patterns_db=home.patterns, activity_db=home.activity)
    assert ev == {"entity_id": "light.nothing", "focal": None, "t0": None, "changes": [],
                  "commands": [], "actions": [], "hour_hits": 0}


def test_gather_latest_focal_and_window(rca, home):
    home.change(ago(1801), "light.hall", "off", "on")          # just outside
    home.change(ago(1800), "light.hall", "on", "off")          # boundary, inside
    home.change(EVENT, "light.desk", "off", "on", hour=21)
    home.change(ago(-120), "light.hall", "off", "on")          # tail boundary, inside
    home.change(ago(-121), "light.hall", "on", "off")          # too late
    home.said(ago(10), "desk on")
    home.did(ago(20), "Turned on the desk light", "light.desk")
    ev = rca.gather("light.desk", patterns_db=home.patterns, activity_db=home.activity)
    assert ev["focal"]["timestamp"] == EVENT
    assert ev["t0"] == dt.datetime.fromisoformat(EVENT)
    assert [r["timestamp"] for r in ev["changes"]] == [ago(1800), EVENT, ago(-120)]
    assert [r["text"] for r in ev["commands"]] == ["desk on"]
    assert [r["message"] for r in ev["actions"]] == ["Turned on the desk light"]


def test_gather_custom_window(rca, home):
    home.change(ago(100), "light.hall", "on", "off")
    home.change(EVENT, "light.desk", "off", "on")
    ev = rca.gather("light.desk", window_secs=60, patterns_db=home.patterns,
                    activity_db=home.activity)
    assert [r["entity_id"] for r in ev["changes"]] == ["light.desk"]


def test_gather_event_time_picks_row_at_or_before(rca, home):
    home.change("2026-03-14 08:00:00", "lock.back", "locked", "unlocked")
    home.change("2026-03-14T12:00:00", "lock.back", "unlocked", "locked")
    ev = rca.gather("lock.back", "2026-03-14 12:00:00", patterns_db=home.patterns,
                    activity_db=home.activity)
    assert ev["focal"]["new_state"] == "locked"
    ev = rca.gather("lock.back", "2026-03-14 11:59:59", patterns_db=home.patterns,
                    activity_db=home.activity)
    assert ev["focal"]["new_state"] == "unlocked"


def test_gather_synthesises_focal_for_event_time_without_rows(rca, home):
    home.change(ago(30), "switch.fan_plug", "on", "unavailable")
    ev = rca.gather("fan.bedroom", EVENT, patterns_db=home.patterns, activity_db=home.activity)
    assert ev["focal"] == {"entity_id": "fan.bedroom", "timestamp": EVENT, "old_state": None,
                           "new_state": None, "area_id": None, "domain": "fan",
                           "triggered_by": None, "hour": None}
    assert len(ev["changes"]) == 1
    assert ev["hour_hits"] == 0


def test_gather_unparsable_focal_time_returns_bare_evidence(rca, home):
    ev = rca.gather("light.x", "not a time", patterns_db=home.patterns, activity_db=home.activity)
    assert ev["t0"] is None and ev["changes"] == []


def test_hour_hits_counts_rows_in_horizon_excluding_focal(rca, home):
    for day in (1, 5, 10, 13, 13):                              # rows, not days
        home.change(f"2026-03-{day:02d} 21:00:00", "climate.loft", "idle", "heat", hour=21)
    home.change("2026-02-27 21:00:00", "climate.loft", "idle", "heat", hour=21)  # > 14 days
    home.change("2026-03-12 21:00:00", "climate.loft", "idle", "cool", hour=21)  # other state
    home.change("2026-03-12 07:00:00", "climate.loft", "idle", "heat", hour=7)   # other hour
    home.change(EVENT, "climate.loft", "idle", "heat", hour=21)
    ev = rca.gather("climate.loft", patterns_db=home.patterns, activity_db=home.activity)
    assert ev["hour_hits"] == 5


def test_hour_hits_skipped_without_hour(rca, home):
    for day in range(5, 12):
        home.change(f"2026-03-{day:02d} 21:00:00", "climate.loft", "idle", "heat")
    home.change(EVENT, "climate.loft", "idle", "heat")
    ev = rca.gather("climate.loft", patterns_db=home.patterns, activity_db=home.activity)
    assert ev["hour_hits"] == 0


# ── candidates ───────────────────────────────────────────────────────────────

def test_no_history_candidate(rca):
    assert rca.candidates({"entity_id": "x.y", "focal": None, "t0": None}) == [{
        "kind": "unknown", "confidence": 0.0, "cause": "no recorded history for this entity",
        "evidence": "the state logger has no rows for it yet"}]


def test_attributed(rca, home):
    home.change(EVENT, "cover.patio", "open", "closed", by="  automation.dusk  ")
    top = home.why(rca, "cover.patio")["candidates"][0]
    assert top == {"kind": "attributed", "confidence": 0.95,
                   "cause": "triggered by 'automation.dusk'",
                   "evidence": "recorded by the state logger at the moment of the change"}


@pytest.mark.parametrize("by", ["system", "SYSTEM", "", "   ", None])
def test_system_or_empty_trigger_is_not_attributed(rca, home, by):
    home.change(EVENT, "cover.patio", "open", "closed", by=by)
    assert "attributed" not in kinds(home.why(rca, "cover.patio"))


def test_cascade_by_shared_name(rca, home):
    home.change("2026-03-14T20:59:15", "sensor.attic_bridge_signal", "ok", "unavailable")
    home.change(EVENT, "binary_sensor.attic_bridge_door", "off", "unavailable")
    top = home.why(rca, "binary_sensor.attic_bridge_door")["candidates"][0]
    assert top == {"kind": "cascade", "confidence": 0.9,
                   "cause": "sensor.attic_bridge_signal went unavailable 45s earlier",
                   "evidence": "shares the device name 'attic' — likely an upstream device "
                               "or hub failure",
                   "timestamp": "2026-03-14T20:59:15"}


def test_cascade_by_same_room(rca, home):
    home.change(ago(60), "switch.power_strip", "on", "Unknown", area="study")
    home.change(EVENT, "media_player.desk_speaker", "idle", "unavailable", area="study")
    top = home.why(rca, "media_player.desk_speaker")["candidates"][0]
    assert top["kind"] == "cascade" and top["confidence"] == 0.8
    assert top["cause"] == "switch.power_strip went Unknown 60s earlier"
    assert top["evidence"] == "same room — likely an upstream device or hub failure"


def test_cascade_limits(rca, home):
    home.change(ago(91), "sensor.attic_bridge_a", "ok", "unavailable")      # too early
    home.change(ago(30), "sensor.attic_bridge_b", "ok", "offline")          # not unavailable
    home.change(ago(20), "sensor.cellar_probe", "ok", "unavailable")        # unrelated
    home.change(ago(10), "binary_sensor.attic_bridge_door", "on", "unavailable")  # own row
    home.change(EVENT, "binary_sensor.attic_bridge_door", "off", "unavailable")
    assert "cascade" not in kinds(home.why(rca, "binary_sensor.attic_bridge_door"))


def test_every_qualifying_cascade_row_counts(rca, home):
    home.change(ago(80), "sensor.attic_bridge_a", "ok", "unavailable")
    home.change(ago(40), "sensor.attic_bridge_b", "ok", "unavailable")
    home.change(EVENT, "binary_sensor.attic_bridge_door", "off", "unavailable")
    res = home.why(rca, "binary_sensor.attic_bridge_door")
    assert [c["cause"] for c in res["candidates"]] == [
        "sensor.attic_bridge_a went unavailable 80s earlier",
        "sensor.attic_bridge_b went unavailable 40s earlier"]


def test_command_naming_device(rca, home):
    home.said(ago(120), "Turn OFF the Porch light", person="alex")
    home.change(EVENT, "light.porch", "on", "off")
    top = home.why(rca, "light.porch")["candidates"][0]
    assert top == {"kind": "command", "confidence": 0.85,
                   "cause": 'alex asked: "Turn OFF the Porch light" 120s earlier',
                   "evidence": "the request names this device, its room, or its type",
                   "timestamp": ago(120)}


def test_command_naming_room_or_type(rca, home):
    home.said(ago(50), "make the nursery darker", person="sam")
    home.change(EVENT, "cover.blind_one", "open", "closed", area="nursery")
    assert kinds(home.why(rca, "cover.blind_one"))[0] == "command"


def test_command_matches_domain_word_anywhere(rca, home):
    home.said(ago(50), "are any lights still on?", person="sam")
    home.change(EVENT, "light.xyz", "on", "off")
    assert kinds(home.why(rca, "light.xyz")) == ["command"]


def test_command_person_default_and_window(rca, home):
    home.said(ago(301), "porch off", person="alex")      # too early
    home.said(ago(10), "porch off")                      # table default person
    home.said(ago(-5), "porch off", person="later")      # after the event
    home.change(EVENT, "light.porch", "on", "off")
    res = home.why(rca, "light.porch")
    assert [c["cause"] for c in res["candidates"]] == ['unknown asked: "porch off" 10s earlier']


def test_command_with_empty_person_reads_someone(rca, home):
    home.said(ago(10), "porch off", person="")
    home.change(EVENT, "light.porch", "on", "off")
    assert home.why(rca, "light.porch")["candidates"][0]["cause"].startswith("someone asked:")


def test_unrelated_command_is_ignored(rca, home):
    home.said(ago(10), "what's the weather", person="alex")
    home.change(EVENT, "light.porch", "on", "off")
    assert kinds(home.why(rca, "light.porch")) == ["unknown"]


def test_nova_action_by_entity(rca, home):
    long = "Secured the house for the night " + "x" * 200
    home.did(ago(45), long, eid="lock.side_gate", category="lockdown")
    home.change(EVENT, "lock.side_gate", "unlocked", "locked")
    top = home.why(rca, "lock.side_gate")["candidates"][0]
    assert top == {"kind": "nova", "confidence": 0.8,
                   "cause": f'Nova acted 45s earlier: "{long[:120]}"',
                   "evidence": "category lockdown", "timestamp": ago(45)}


def test_nova_action_by_message_token_or_area(rca, home):
    home.did(ago(45), "Dimmed the GALLERY for the film")
    home.change(EVENT, "light.ceiling_one", "on", "off", area="Gallery")
    assert kinds(home.why(rca, "light.ceiling_one")) == ["nova"]
    assert home.why(rca, "light.ceiling_one")["candidates"][0]["evidence"] == "category other"


def test_nova_action_too_old_is_ignored(rca, home):
    home.did(ago(301), "locked side gate", eid="lock.side_gate")
    home.change(EVENT, "lock.side_gate", "unlocked", "locked")
    assert kinds(home.why(rca, "lock.side_gate")) == ["unknown"]


def test_schedule(rca, home):
    for day in range(10, 14):
        home.change(f"2026-03-{day} 21:00:00", "switch.towel_rail", "off", "on", hour=21)
    home.change(EVENT, "switch.towel_rail", "off", "on", hour=21)
    top = home.why(rca, "switch.towel_rail")["candidates"][0]
    assert top == {"kind": "schedule", "confidence": 0.55,
                   "cause": "this same change has occurred at this hour on 4 recent days",
                   "evidence": "a recurring pattern — likely a Home Assistant automation "
                               "or schedule"}


def test_three_hits_is_not_a_schedule(rca, home):
    for day in range(11, 14):
        home.change(f"2026-03-{day} 21:00:00", "switch.towel_rail", "off", "on", hour=21)
    home.change(EVENT, "switch.towel_rail", "off", "on", hour=21)
    assert "schedule" not in kinds(home.why(rca, "switch.towel_rail"))


def test_area_clue_is_earliest_in_two_minutes(rca, home):
    home.change(ago(121), "binary_sensor.den_motion_a", "off", "on", area="den")
    home.change(ago(100), "binary_sensor.den_motion_b", "off", "on", area="den")
    home.change(ago(10), "binary_sensor.den_motion_c", "off", "on", area="den")
    home.change(ago(5), "light.hall", "off", "on", area="hall")
    home.change(EVENT, "light.reading_lamp", "off", "on", area="den")
    res = home.why(rca, "light.reading_lamp")
    assert res["candidates"] == [{
        "kind": "area", "confidence": 0.5,
        "cause": "binary_sensor.den_motion_b → on 100s earlier in the same room",
        "evidence": "related activity in the same area", "timestamp": ago(100)}]


def test_no_area_no_area_clue(rca, home):
    home.change(ago(10), "binary_sensor.motion_x", "off", "on")
    home.change(EVENT, "light.reading_lamp", "off", "on")
    assert kinds(home.why(rca, "light.reading_lamp")) == ["unknown"]


def test_unknown_when_nothing_nearby(rca, home):
    home.change(EVENT, "light.reading_lamp", "off", "on")
    assert home.why(rca, "light.reading_lamp")["candidates"] == [{
        "kind": "unknown", "confidence": 0.0, "cause": "no plausible cause found in the window",
        "evidence": "no attributed trigger, upstream failure, command, Nova action, or "
                    "recurring pattern nearby"}]


def test_ranking_is_stable_and_capped(rca, home):
    home.change(ago(60), "sensor.loft_hub_a", "ok", "unavailable", area="loft")
    home.change(ago(50), "sensor.loft_hub_b", "ok", "unavailable", area="loft")
    home.change(ago(40), "sensor.other_thing", "ok", "unavailable", area="loft")
    home.said(ago(30), "loft hub restart", person="alex")
    home.did(ago(20), "restarted the loft hub")
    home.change(EVENT, "switch.loft_hub_plug", "on", "off", area="loft",
                by="automation.watchdog")
    res = home.why(rca, "switch.loft_hub_plug")
    assert [(c["kind"], c["confidence"]) for c in res["candidates"]] == [
        ("attributed", 0.95), ("cascade", 0.9), ("cascade", 0.9), ("command", 0.85),
        ("cascade", 0.8)]
    assert len(res["candidates"]) == rca.CANDIDATE_CAP


# ── names ────────────────────────────────────────────────────────────────────

def test_names_word_causes_timeline_and_summary(rca, home):
    home.change(ago(20), "switch.shed_plug", "on", "unavailable", area="shed")
    home.change(EVENT, "light.shed_lamp", "on", "unavailable", area="shed")
    names = {"switch.shed_plug": "Shed Plug"}
    res = home.why(rca, "light.shed_lamp", names=names)
    assert res["entity_id"] == "light.shed_lamp"
    assert res["candidates"][0]["cause"] == "Shed Plug went unavailable 20s earlier"
    assert res["summary"].startswith("Most likely cause: Shed Plug went unavailable")
    texts = [i["text"] for i in res["timeline"]]
    assert texts == ["Shed Plug: on → unavailable", "shed lamp: on → unavailable"]


def test_unknown_summary_uses_name_or_id(rca, home):
    res = home.why(rca, "light.garden_spot")
    assert res["summary"] == ("I couldn't determine why light.garden_spot changed — "
                              "the state logger has no rows for it yet.")
    res = home.why(rca, "light.garden_spot", names={"light.garden_spot": "Garden Spot"})
    assert res["summary"].startswith("I couldn't determine why Garden Spot changed")
    res = home.why(rca, "light.garden_spot", names={})
    assert res["summary"].startswith("I couldn't determine why garden spot changed")


# ── timeline ─────────────────────────────────────────────────────────────────

def test_timeline_merges_sources_in_time_order(rca, home):
    home.said("2026-03-14T20:58:00", "movie time", person="alex")
    home.did(ago(90), "Starting movie mode " + "y" * 200)
    home.change(ago(30), "light.hall", None, "off")
    home.change(EVENT, "light.lounge", "on", "off")
    tl = home.why(rca, "light.lounge")["timeline"]
    assert [i["src"] for i in tl] == ["command", "nova", "change", "change"]
    assert tl[0] == {"t": "2026-03-14T20:58:00", "src": "command", "text": 'alex: "movie time"'}
    assert tl[1]["text"] == ("Starting movie mode " + "y" * 200)[:140]
    assert tl[2]["text"] == "light.hall: None → off"


def test_timeline_null_person_prints_none(rca, home):
    with sqlite3.connect(home.patterns) as c:
        c.execute("INSERT INTO commands (timestamp, text, person) VALUES (?,?,NULL)",
                  (ago(10), "hello"))
    home.change(EVENT, "light.lounge", "on", "off")
    tl = home.why(rca, "light.lounge")["timeline"]
    assert tl[0]["text"] == 'None: "hello"'


def test_timeline_keeps_last_forty(rca, home):
    for i in range(45):
        home.change(ago(1000 - i), f"sensor.s{i}", "1", "2")
    home.change(EVENT, "light.lounge", "on", "off")
    tl = home.why(rca, "light.lounge")["timeline"]
    assert len(tl) == rca.TIMELINE_CAP
    assert tl[-1]["text"] == "light.lounge: on → off"
    assert tl[0]["text"] == "sensor.s6: 1 → 2"


# ── analyze ──────────────────────────────────────────────────────────────────

def test_analyze_shape_and_summary(rca, home):
    home.change(EVENT, "light.porch", "on", "off", area="front", by="automation.dawn")
    res = home.why(rca, "light.porch")
    assert set(res) == {"entity_id", "event", "candidates", "timeline", "summary"}
    assert res["event"] == {"timestamp": EVENT, "old_state": "on", "new_state": "off",
                            "area_id": "front"}
    assert res["summary"] == ("Most likely cause: triggered by 'automation.dawn' (95% confident"
                              " — recorded by the state logger at the moment of the change).")


def test_analyze_percent_is_truncated(rca, home):
    for day in range(9, 14):
        home.change(f"2026-03-{day:02d} 21:00:00", "switch.towel_rail", "off", "on", hour=21)
    home.change(EVENT, "switch.towel_rail", "off", "on", hour=21)
    assert "(55% confident — " in home.why(rca, "switch.towel_rail")["summary"]


def test_analyze_event_time_positional(rca, home):
    home.change("2026-03-14 06:00:00", "light.porch", "off", "on", by="automation.early")
    home.change(EVENT, "light.porch", "on", "off")
    res = home.why(rca, "light.porch", "2026-03-14 07:00:00", 600)
    assert res["event"]["new_state"] == "on"
    assert "automation.early" in res["summary"]


def test_analyze_without_history_has_empty_event(rca, home):
    res = home.why(rca, "light.void")
    assert res["event"] == {"timestamp": None, "old_state": None, "new_state": None,
                            "area_id": None}
    assert res["timeline"] == []


def test_analyze_missing_databases(rca, tmp_path):
    res = rca.analyze("light.x", patterns_db=str(tmp_path / "a" / "p.db"),
                      activity_db=str(tmp_path / "b" / "c.db"))
    assert kinds(res) == ["unknown"]


def test_analyze_backstop(rca, home, monkeypatch, caplog):
    def boom(*a, **k):
        raise ValueError("disk on fire")
    monkeypatch.setattr(rca, "gather", boom)
    with caplog.at_level(logging.ERROR):
        res = home.why(rca, "light.porch")
    assert res == {"entity_id": "light.porch", "event": {}, "candidates": [],
                   "timeline": [], "summary": "analysis failed: disk on fire"}
    assert any("rca.analyze failed" in r.getMessage() for r in caplog.records)
