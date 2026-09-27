"""Suggestion quality (v7.126.0): a stricter threshold detector, "already
automated" by effect, and the opt-in AI review whose rejections are never
suggested again.

The threshold cases reproduce what the panel showed on a real install:
every evening the landing, living room and bedroom get dark around the time
the kitchen light goes on, so each dark sensor became a "trigger" for the
kitchen light, repeated once per sensor at "100% confident".
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def pa(load):
    return load("automation.patterns")


def _db(load, path):
    ps = load("persistence.sqlite")
    conn = sqlite3.connect(path)
    ps.ensure(conn, "pattern_log")
    conn.commit()
    return conn


def _ins(conn, eid, st, when, triggered_by="device_or_integration"):
    conn.execute(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
        "new_state, area_id, hour, day_of_week, person, triggered_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (when.isoformat(), eid, eid.split(".")[0], "off", st, "",
         when.hour, when.weekday(), "unknown", triggered_by))


def _evening_house(conn, days=14):
    """The kitchen light goes on at 19:30 every evening. Landing lux falls
    at dusk (19:00, half an hour earlier) and stays low; kitchen lux drops
    two minutes before the light, when the blinds are drawn."""
    base = (datetime.now() - timedelta(days=days + 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    light_times, landing, kitchen = [], [], []
    for d in range(days):
        day = base + timedelta(days=d)
        on = day.replace(hour=19, minute=30)
        _ins(conn, "switch.kitchen_light", "on", on)
        light_times.append(on.timestamp())
        dark_from = on - timedelta(minutes=2)
        for q in range(96):
            t = day + timedelta(minutes=15 * q)
            landing.append((t.timestamp(), 200.0 if 6 <= t.hour < 19 else 20.0))
            kitchen.append((t.timestamp(),
                            150.0 if (t.hour >= 7 and t < dark_from) else 10.0))
        kitchen.append((dark_from.timestamp(), 10.0))
    conn.commit()
    return light_times, {"sensor.landing_illuminance": landing,
                         "sensor.kitchen_illuminance": kitchen}


AREAS = {"switch.kitchen_light": "kitchen",
         "sensor.kitchen_illuminance": "kitchen",
         "sensor.kitchen_other_illuminance": "kitchen",
         "sensor.landing_illuminance": "landing",
         "sensor.living_room_illuminance": "living_room"}


# ── the stricter threshold detector ─────────────────────────────────────

def test_sensor_in_another_room_is_never_a_trigger(pa, load, tmp_path):
    conn = _db(load, tmp_path / "p.db")
    conn.row_factory = sqlite3.Row
    _t, hist = _evening_house(conn)
    only_landing = {"sensor.landing_illuminance": hist["sensor.landing_illuminance"]}
    pats = pa.PatternAnalyzer()._find_numeric_triggers(conn, only_landing,
                                                         entity_areas=AREAS)
    assert pats == []


def test_the_landing_sensor_is_what_the_old_rule_accepted(pa):
    """Values at the light's times sit in the low part of the landing
    sensor's range, which is all the pre-v7.126.0 detector checked."""
    import statistics
    landing = [200.0 if 6 <= h < 19 else 20.0 for h in range(24) for _ in range(4)]
    assert statistics.median(landing) == 200.0
    assert pa._numeric_trigger_from([20.0] * 14, landing) == {"below": pytest.approx(20, abs=10)}


def test_same_room_needs_a_crossing_shortly_before(pa, load, tmp_path):
    """Put the landing sensor in the kitchen: it is dark at 19:30 every
    evening, but it crossed at 19:00, half an hour earlier, so it doesn't
    explain the light."""
    conn = _db(load, tmp_path / "p.db")
    conn.row_factory = sqlite3.Row
    _t, hist = _evening_house(conn)
    areas = dict(AREAS, **{"sensor.landing_illuminance": "kitchen"})
    only_landing = {"sensor.landing_illuminance": hist["sensor.landing_illuminance"]}
    assert pa.PatternAnalyzer()._find_numeric_triggers(
        conn, only_landing, entity_areas=areas) == []


def test_same_room_crossing_just_before_is_learned_once(pa, load, tmp_path):
    conn = _db(load, tmp_path / "p.db")
    conn.row_factory = sqlite3.Row
    _t, hist = _evening_house(conn)
    # A second kitchen sensor that behaves the same way.
    hist["sensor.kitchen_other_illuminance"] = list(hist["sensor.kitchen_illuminance"])
    pats = pa.PatternAnalyzer()._find_numeric_triggers(conn, hist, entity_areas=AREAS)
    assert len(pats) == 1, [p.description for p in pats]
    p = pats[0]
    assert p.details["action"] == {"entity": "switch.kitchen_light", "state": "on"}
    assert p.details["trigger_sensor"].startswith("sensor.kitchen")
    assert p.details["op"] == "below"
    assert p.details["followed"] == 14 and p.details["crossings"] == 14
    # A real score (support 14 of a possible 15 for full volume), not a
    # count capped at 100%.
    assert p.confidence == pytest.approx(0.933, abs=0.001)


def test_nothing_without_areas(pa, load, tmp_path):
    conn = _db(load, tmp_path / "p.db")
    conn.row_factory = sqlite3.Row
    _t, hist = _evening_house(conn)
    assert pa.PatternAnalyzer()._find_numeric_triggers(conn, hist) == []


def test_threshold_evidence_explains_the_link(load):
    s = load("automation.suggestions")
    why = s.explain_suggestion("numeric_trigger", {
        "trigger_sensor": "sensor.kitchen_illuminance", "op": "below",
        "threshold": 37, "action": {"entity": "switch.kitchen_light", "state": "on"},
        "followed": 14, "crossings": 15, "action_count": 20,
        "window_seconds": 600, "distinct_days": 14,
        "names": {"sensor.kitchen_illuminance": "Kitchen Illuminance",
                  "switch.kitchen_light": "Kitchen Light"}}, 14)
    assert why["headline"] == "A threshold routine"
    assert why["evidence"] == [
        "When Kitchen Illuminance drops below 37, Kitchen Light turns on",
        "Followed within 10 minutes on 14 of 15 times it crossed",
        "Kitchen Light changed this way 20 times in 30 days",
        "On 14 different days",
    ]


# ── "already automated" by effect ───────────────────────────────────────

class _Record:
    def __init__(self, name, config, refs=()):
        self.entity_id = f"automation.{name}"
        self.name = name
        self.raw_config = config
        self.referenced_entities = set(refs)


def test_same_action_from_another_trigger_is_already_automated(pa, load, monkeypatch):
    inv = load("automation.inventory")
    existing = _Record("kitchen_dusk", {
        "trigger": [{"trigger": "sun", "event": "sunset"}],
        "action": [{"action": "switch.turn_on", "entity_id": "switch.kitchen_light"}]})
    monkeypatch.setattr(inv, "get_inventory",
                        lambda hass: type("I", (), {"records": lambda self: [existing]})())
    models = load("automation.models")
    p = models.DetectedPattern(
        "numeric_trigger", "d", ["sensor.kitchen_illuminance", "switch.kitchen_light"],
        0.8, 12, details={"trigger_sensor": "sensor.kitchen_illuminance", "op": "below",
                          "threshold": 37, "action": {"entity": "switch.kitchen_light",
                                                      "state": "on"}})
    match = pa.PatternAnalyzer()._automation_match(object(), p)
    assert match["status"] == "already_automated"
    assert match["matches"][0]["name"] == "kitchen_dusk"


def test_an_automation_that_only_references_the_device_stays_an_overlap(pa, load, monkeypatch):
    inv = load("automation.inventory")
    existing = _Record("kitchen_off_when_empty", {
        "trigger": [{"trigger": "state", "entity_id": "binary_sensor.kitchen_occupancy",
                     "to": "off"}],
        "action": [{"action": "switch.turn_off", "entity_id": "switch.kitchen_light"}]},
        refs=("binary_sensor.kitchen_occupancy", "switch.kitchen_light"))
    monkeypatch.setattr(inv, "get_inventory",
                        lambda hass: type("I", (), {"records": lambda self: [existing]})())
    models = load("automation.models")
    p = models.DetectedPattern(
        "numeric_trigger", "d", ["sensor.kitchen_illuminance", "switch.kitchen_light"],
        0.8, 12, details={"trigger_sensor": "sensor.kitchen_illuminance", "op": "below",
                          "threshold": 37, "action": {"entity": "switch.kitchen_light",
                                                      "state": "on"}})
    match = pa.PatternAnalyzer()._automation_match(object(), p)
    assert match["status"] == "possible_overlap"


# ── the AI review ───────────────────────────────────────────────────────

def _pattern(models, n=1):
    return models.DetectedPattern(
        "sequence", f"When Front Door opens, Hall Light {n} turns on",
        ["binary_sensor.front_door", f"light.hall_{n}"], 0.9, 12,
        details={"trigger": {"entity": "binary_sensor.front_door", "state": "on"},
                 "action": {"entity": f"light.hall_{n}", "state": "on"},
                 "delay_seconds": 30})


@pytest.fixture
def world(pa, load, tmp_path, monkeypatch, fake_hass):
    """An analyzer over a real suggestions table whose detection returns the
    patterns in world["patterns"]."""
    models = load("automation.models")
    db = str(tmp_path / "patterns.db")
    _db(load, db).close()
    an = pa.PatternAnalyzer()
    an._db = db
    state = {"patterns": [_pattern(models)]}

    def _detect(*a, **k):
        return [models.DetectedPattern(p.pattern_type, p.description,
                                       list(p.entity_ids), p.confidence,
                                       p.occurrences, details=dict(p.details))
                for p in state["patterns"]]
    monkeypatch.setattr(an, "_detect_patterns", _detect)
    monkeypatch.setattr(an, "_automation_match", lambda hass, p: {"status": "new",
                                                                  "matches": []})
    monkeypatch.setattr(an, "_store_person_pattern", lambda p: False)
    monkeypatch.setattr(an, "_promote_to_knowledge", lambda pats: 0)
    monkeypatch.setattr(pa, "_learned_threshold_delta", lambda: 0.0)

    async def _none(hass):
        return {}
    monkeypatch.setattr(an, "_fetch_numeric_sensor_history", _none)
    state.update(an=an, db=db, models=models, hass=fake_hass)
    return state


def _rows(db):
    c = sqlite3.connect(db)
    try:
        return [(r[0], json.loads(r[1] or "{}").get("review", {}).get("verdict"))
                for r in c.execute("SELECT status, details FROM suggestions ORDER BY id")]
    finally:
        c.close()


class _Reviewer:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    async def __call__(self, hass, pattern):
        self.calls += 1
        v = self.verdicts.pop(0) if self.verdicts else None
        if v is None:
            return None
        return {"verdict": v, "reason": f"{v} it", "model": "m", "provider": "p"}


async def test_rejected_suggestion_is_never_suggested_again(world):
    an, db = world["an"], world["db"]
    first = _Reviewer(["reject"])
    await an.analyze(world["hass"], reviewer=first)
    assert _rows(db) == [("rejected", "reject")]
    assert an.get_pending_suggestions() == []
    # Detected again, with or without the review on: stays rejected, and the
    # review is not asked again.
    again = _Reviewer(["keep"])
    await an.analyze(world["hass"], reviewer=again)
    await an.analyze(world["hass"])
    assert again.calls == 0
    assert _rows(db) == [("rejected", "reject")]
    assert an.get_pending_suggestions() == []


async def test_kept_suggestion_is_shown_and_not_reviewed_again(world):
    an, db = world["an"], world["db"]
    first = _Reviewer(["keep"])
    await an.analyze(world["hass"], reviewer=first)
    assert _rows(db) == [("pending", "keep")]
    later = _Reviewer(["reject"])
    await an.analyze(world["hass"], reviewer=later)
    assert later.calls == 0
    assert _rows(db) == [("pending", "keep")]


async def test_unjudged_suggestion_waits_for_the_next_pass(world):
    an, db = world["an"], world["db"]
    await an.analyze(world["hass"], reviewer=_Reviewer([None]))
    assert _rows(db) == []
    assert an._last_result["review_deferred"] == 1
    await an.analyze(world["hass"], reviewer=_Reviewer(["keep"]))
    assert _rows(db) == [("pending", "keep")]


async def test_existing_unreviewed_suggestion_is_reviewed_once(world):
    an, db = world["an"], world["db"]
    await an.analyze(world["hass"])                        # review off
    assert _rows(db) == [("pending", None)]
    await an.analyze(world["hass"], reviewer=_Reviewer(["reject"]))
    assert _rows(db) == [("rejected", "reject")]


async def test_review_is_capped_per_pass(world, pa):
    models = world["models"]
    world["patterns"] = [_pattern(models, n) for n in range(pa.REVIEW_MAX_PER_PASS + 3)]
    reviewer = _Reviewer(["keep"] * 50)
    await world["an"].analyze(world["hass"], reviewer=reviewer)
    assert reviewer.calls == pa.REVIEW_MAX_PER_PASS
    assert len(_rows(world["db"])) == pa.REVIEW_MAX_PER_PASS
    assert world["an"]._last_result["review_deferred"] == 3


async def test_restored_suggestion_is_back_and_not_reviewed_again(world):
    an, db = world["an"], world["db"]
    await an.analyze(world["hass"], reviewer=_Reviewer(["reject"]))
    (row,) = an.get_rejected_suggestions()
    assert an.restore_suggestion(row["id"]) is True
    assert [s["id"] for s in an.get_pending_suggestions()] == [row["id"]]
    again = _Reviewer(["reject"])
    await an.analyze(world["hass"], reviewer=again)
    assert again.calls == 0
    assert _rows(db) == [("pending", "reject")]


async def test_a_dismissed_suggestion_is_never_reviewed(world):
    an, db = world["an"], world["db"]
    await an.analyze(world["hass"])
    (s,) = an.get_pending_suggestions()
    an.dismiss_suggestion(s["id"])
    reviewer = _Reviewer(["keep"])
    await an.analyze(world["hass"], reviewer=reviewer)
    assert reviewer.calls == 0
    assert _rows(db)[0][0] == "dismissed"


# ── old threshold suggestions are retired, and come back if detected ────

def _numeric(models):
    return models.DetectedPattern(
        "numeric_trigger", "When Landing goes below 70, Kitchen Light turns on",
        ["sensor.landing_illuminance", "switch.kitchen_light"], 0.9, 20,
        details={"trigger_sensor": "sensor.landing_illuminance", "op": "below",
                 "threshold": 70, "action": {"entity": "switch.kitchen_light",
                                             "state": "on"}})


async def test_threshold_suggestion_no_longer_found_is_retired(world, monkeypatch):
    an, db, models = world["an"], world["db"], world["models"]
    world["patterns"] = [_numeric(models)]
    await an.analyze(world["hass"])
    assert _rows(db) == [("pending", None)]
    world["patterns"] = []

    async def _hist(hass):
        return {"sensor.landing_illuminance": [(0, 1.0)]}
    monkeypatch.setattr(an, "_fetch_numeric_sensor_history", _hist)
    orig = an._detect_patterns

    def _detect(*a, **k):
        an._numeric_ran = True
        an._completed_types.add("numeric_trigger")
        return orig(*a, **k)
    monkeypatch.setattr(an, "_detect_patterns", _detect)
    await an.analyze(world["hass"])
    assert _rows(db) == [("retired", None)]
    assert an.get_pending_suggestions() == []
    world["patterns"] = [_numeric(models)]
    await an.analyze(world["hass"])
    assert _rows(db) == [("pending", None)]


async def test_not_retired_when_the_threshold_detector_did_not_run(world):
    an, db, models = world["an"], world["db"], world["models"]
    world["patterns"] = [_numeric(models)]
    await an.analyze(world["hass"])
    world["patterns"] = []
    await an.analyze(world["hass"])                 # no sensor history this pass
    assert _rows(db) == [("pending", None)]


# ── the review module ───────────────────────────────────────────────────

def test_verdict_parsing(load):
    sr = load("suggestion_review")
    assert sr.parse_verdict('{"keep": false, "reason": "Different rooms."}') == {
        "verdict": "reject", "reason": "Different rooms."}
    assert sr.parse_verdict('```json\n{"keep": true, "reason": "ok"}\n```')["verdict"] == "keep"
    for bad in ("", "no", '{"keep": "yes"}', '{"reason": "x"}', None, "[1]"):
        assert sr.parse_verdict(bad) is None, bad


def test_review_is_off_by_default_and_follows_the_main_agent(load):
    sr = load("suggestion_review")
    assert sr.enabled({}) is False
    assert sr.enabled({"suggestion_review_enabled": True}) is True
    assert sr.role_choice({"llm_provider": "ollama", "model": "gemma4:26b"}) == (
        "ollama", "gemma4:26b")
    assert sr.role_choice({"llm_provider": "ollama", "model": "gemma4:26b",
                           "suggestion_review_provider": "groq",
                           "suggestion_review_model": "x"}) == ("groq", "x")


def test_prompt_names_entities_and_fences_home_data(load):
    sr = load("suggestion_review")
    models = load("automation.models")
    p = models.DetectedPattern(
        "sequence", "When binary_sensor.front_door turns on, light.hall turns on",
        ["binary_sensor.front_door", "light.hall"], 0.9, 12,
        details={"trigger": {"entity": "binary_sensor.front_door", "state": "on"},
                 "action": {"entity": "light.hall", "state": "on"},
                 "names": {"binary_sensor.front_door": "Front Door",
                           "light.hall": "Hall Light"}})
    fenced = []

    def fence(body, **kw):
        fenced.append(body)
        return f"<<{body}>>"
    msgs = sr.build_messages(p, fence=fence)
    assert msgs[0]["role"] == "system" and "JSON" in msgs[0]["content"]
    data = json.loads(fenced[0])
    assert data["suggestion"] == "When Front Door turns on, Hall Light turns on"
    assert msgs[1]["content"] == f"<<{fenced[0]}>>"


async def test_no_reviewer_when_disabled(load, monkeypatch, fake_hass):
    sr = load("suggestion_review")

    async def _cfg(hass):
        return {"suggestion_review_enabled": False}
    monkeypatch.setattr(sr, "_config", _cfg)
    assert await sr.reviewer_for(fake_hass) is None


# ── panel payloads ──────────────────────────────────────────────────────

def test_filtered_items_show_names_and_reason(load):
    api = load("automation.api")
    (item,) = api.panel_rejected_items([{
        "id": 3, "pattern_type": "numeric_trigger",
        "description": "When sensor.landing_illuminance goes below 70, "
                       "switch.kitchen_light turns on",
        "details": json.dumps({"review": {"verdict": "reject",
                                          "reason": "Different rooms.",
                                          "model": "gemma4:26b", "ts": "t"}})}],
        {"sensor.landing_illuminance": "Landing Illuminance",
         "switch.kitchen_light": "Kitchen Light"})
    assert item == {"id": 3, "pattern_type": "numeric_trigger",
                    "description": "When Landing Illuminance goes below 70, "
                                   "Kitchen Light turns on",
                    "reason": "Different rooms.", "model": "gemma4:26b",
                    "reviewed_at": "t"}


def test_chips_carry_names(load):
    api = load("automation.api")
    (item,) = api.panel_suggestion_items([{
        "id": 1, "entity_ids": json.dumps(["switch.kitchen_light"]),
        "details": "{}", "pattern_type": "numeric_trigger"}],
        {"switch.kitchen_light": "Kitchen Light"})
    assert item["entities"] == ["switch.kitchen_light"]
    assert item["entity_labels"] == ["Kitchen Light"]


async def test_review_call_returns_the_verdict_with_its_model(load, monkeypatch, fake_hass):
    import contextlib
    import types
    sr = load("suggestion_review")
    act = load("providers.activity")
    mgr = load("providers.manager")
    routing = load("providers.routing")
    models = load("automation.models")
    seen = {}

    class _Manager:
        @contextlib.asynccontextmanager
        async def lease(self, spec, binding, factory):
            seen["binding"] = binding
            yield object()

    @contextlib.asynccontextmanager
    async def _scope(hass, entry=None):
        yield _Manager()

    async def _chat(hass, client, messages, **kw):
        seen["role"] = kw["role"]
        seen["model"] = kw["model_override"]
        return types.SimpleNamespace(text='{"keep": false, "reason": "Different rooms."}')

    monkeypatch.setattr(mgr, "provider_scope", _scope)
    monkeypatch.setattr(act, "execute_chat", _chat)
    spec = routing.ProviderSpec(provider="ollama", model="gemma4:26b",
                                base_url="http://ollama.local:11434")
    verdict = await sr.review(fake_hass, _pattern(models), spec)
    assert verdict["verdict"] == "reject" and verdict["reason"] == "Different rooms."
    assert verdict["provider"] == "ollama" and verdict["model"] == "gemma4:26b"
    assert seen == {"binding": "suggestion_review", "role": "suggestion_review",
                    "model": "gemma4:26b"}

    async def _boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(act, "execute_chat", _boom)
    assert await sr.review(fake_hass, _pattern(models), spec) is None


async def test_enabled_without_a_usable_role_waits_instead_of_showing(load, monkeypatch, fake_hass):
    sr = load("suggestion_review")

    async def _cfg(hass):
        # Review on, Main Agent on Groq, but no Groq key saved.
        return {"suggestion_review_enabled": True, "llm_provider": "groq", "model": "x"}
    monkeypatch.setattr(sr, "_config", _cfg)
    reviewer = await sr.reviewer_for(fake_hass)
    assert reviewer is not None
    assert await reviewer(fake_hass, object()) is None


# ── v7.126.1: every detector, not just thresholds ───────────────────────

def _days(n=12):
    base = (datetime.now() - timedelta(days=n + 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return [base + timedelta(days=d) for d in range(n)]


def _seq_db(load, tmp_path, pairs, extra_triggers=0):
    """pairs: [(trigger, trigger_state, action, action_state)] happening every
    evening at 20:00 (+45 s). extra_triggers: trigger events per day that are
    NOT followed by the action (lowers how often the trigger is followed)."""
    conn = _db(load, tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    for day in _days():
        for n, (a, sa, b, sb) in enumerate(pairs):
            t = day.replace(hour=20, minute=0) + timedelta(minutes=30 * n)
            _ins(conn, a, sa, t)
            _ins(conn, b, sb, t + timedelta(seconds=45))
            for k in range(extra_triggers):
                _ins(conn, a, sa, day.replace(hour=8 + k, minute=0))
    conn.commit()
    return conn


def _seq(pa, conn, areas):
    return pa.PatternAnalyzer()._find_sequence_patterns(conn, entity_areas=areas)


def test_sequence_across_rooms_needs_to_hold_almost_every_time(pa, load, tmp_path):
    """Followed one time in two: fine within a room, not across rooms."""
    conn = _seq_db(load, tmp_path, [("binary_sensor.landing_door", "on",
                                     "light.kitchen", "on")], extra_triggers=1)
    areas = {"binary_sensor.landing_door": "landing", "light.kitchen": "kitchen"}
    assert _seq(pa, conn, areas) == []
    same = {"binary_sensor.landing_door": "kitchen", "light.kitchen": "kitchen"}
    (p,) = _seq(pa, conn, same)
    assert p.details["action"] == {"entity": "light.kitchen", "state": "on"}
    # Every single evening, across rooms: believable.
    every = tmp_path / "every"
    every.mkdir()
    conn2 = _seq_db(load, every, [("binary_sensor.landing_door", "on",
                                   "light.kitchen", "on")])
    assert len(_seq(pa, conn2, areas)) == 1


def test_sequence_needs_the_trigger_followed_most_of_the_time(pa, load, tmp_path):
    """The trigger fires twice more every day without the light: followed
    1 time in 3 (33%). The old bar (30%) accepted that; an automation would
    then switch the light on at 08:00 and 09:00 too."""
    conn = _seq_db(load, tmp_path, [("binary_sensor.hall_door", "on",
                                     "light.hall", "on")], extra_triggers=2)
    areas = {"binary_sensor.hall_door": "hall", "light.hall": "hall"}
    assert _seq(pa, conn, areas) == []


def test_sequence_without_areas_needs_stronger_evidence(pa, load, tmp_path):
    conn = _seq_db(load, tmp_path, [("binary_sensor.hall_door", "on",
                                     "light.hall", "on")], extra_triggers=0)
    assert len(_seq(pa, conn, {})) == 1                      # followed every time
    other = tmp_path / "other"
    other.mkdir()
    conn2 = _seq_db(load, other, [("binary_sensor.b_door", "on",
                                   "light.b", "on")], extra_triggers=1)
    assert _seq(pa, conn2, {}) == []                         # followed 1 in 2


def test_arrival_trigger_needs_no_area(pa, load, tmp_path):
    conn = _seq_db(load, tmp_path, [("person.abi", "home", "light.hall", "on")])
    (p,) = _seq(pa, conn, {"light.hall": "hall"})
    assert p.details["trigger"] == {"entity": "person.abi", "state": "home"}


def test_one_sequence_suggestion_per_device(pa, load, tmp_path):
    conn = _seq_db(load, tmp_path, [
        ("binary_sensor.hall_door", "on", "light.hall", "on"),
        ("binary_sensor.hall_motion", "on", "light.hall", "on")])
    areas = {"binary_sensor.hall_door": "hall", "binary_sensor.hall_motion": "hall",
             "light.hall": "hall"}
    pats = _seq(pa, conn, areas)
    assert [p.details["action"]["entity"] for p in pats] == ["light.hall"]


def test_sequence_to_a_read_only_device_is_not_suggested(pa, load, tmp_path):
    conn = _seq_db(load, tmp_path, [("binary_sensor.hall_door", "on",
                                     "binary_sensor.hall_motion", "on")])
    areas = {"binary_sensor.hall_door": "hall", "binary_sensor.hall_motion": "hall"}
    assert _seq(pa, conn, areas) == []


def test_time_routine_for_a_sensor_is_advice_not_a_broken_automation(load):
    s = load("automation.suggestions")
    m = load("automation.models")
    p = m.DetectedPattern("time_routine", "d", ["binary_sensor.hall_motion"], 0.9, 12,
                          details={"hour": 18, "state": "on"})
    auto = json.loads(s.generate_automation(p))
    assert auto.get("type") == "manual_review"          # was binary_sensor.turn_on
    assert s.normalize_suggestion_automation(json.dumps(auto))["installable"] is False


def test_time_routine_for_blinds_is_now_an_automation(load):
    s = load("automation.suggestions")
    m = load("automation.models")
    p = m.DetectedPattern("time_routine", "d", ["cover.bedroom_blinds"], 0.9, 12,
                          details={"hour": 7, "state": "open"})
    auto = json.loads(s.generate_automation(p))
    assert auto["action"] == {"service": "cover.open_cover",
                              "entity_id": "cover.bedroom_blinds"}


async def test_only_automatable_suggestions_reach_the_panel(world):
    """A voice-command routine and a sensor routine are not suggestions; the
    light routine is."""
    m = world["models"]
    world["patterns"] = [
        m.DetectedPattern("repeated_command", "'lights off' around 22:00",
                          [], 0.9, 12, details={"command": "lights off", "hour": 22}),
        m.DetectedPattern("time_routine", "motion at 18:00",
                          ["binary_sensor.hall_motion"], 0.9, 12,
                          details={"hour": 18, "state": "on"}),
        m.DetectedPattern("time_routine", "porch at 18:00", ["light.porch"], 0.9, 12,
                          details={"hour": 18, "state": "on"}),
    ]
    await world["an"].analyze(world["hass"])
    assert [s["pattern_type"] for s in world["an"].get_pending_suggestions()] == [
        "time_routine"]
    assert json.loads(world["an"].get_pending_suggestions()[0]["entity_ids"]) == [
        "light.porch"]


async def test_old_suggestions_the_detectors_no_longer_support_are_retired(world, monkeypatch):
    """Pending rows from before (a presence pairing, a voice command, a
    sequence the stricter rules drop) are hidden once the detectors have run,
    and a supported one stays."""
    an, db, m = world["an"], world["db"], world["models"]
    old = [
        m.DetectedPattern("presence", "When Abi arrives, phone turns home",
                          ["person.abi", "device_tracker.abi_phone"], 0.9, 5,
                          details={"trigger_person": "person.abi", "trigger_state": "home",
                                   "action_entity": "light.hall", "action_state": "on"}),
        m.DetectedPattern("sequence", "landing door then kitchen light",
                          ["binary_sensor.landing_door", "light.kitchen"], 0.9, 12,
                          details={"trigger": {"entity": "binary_sensor.landing_door",
                                               "state": "on"},
                                   "action": {"entity": "light.kitchen", "state": "on"}}),
        m.DetectedPattern("time_routine", "porch at 18:00", ["light.porch"], 0.9, 12,
                          details={"hour": 18, "state": "on"}),
    ]
    for p in old:
        an._suggestions().store(p)
    world["patterns"] = [old[2]]
    orig = an._detect_patterns

    def _detect(*a, **k):
        an._completed_types.update({"presence", "sequence", "time_routine",
                                    "repeated_command"})
        return orig(*a, **k)
    monkeypatch.setattr(an, "_detect_patterns", _detect)
    await an.analyze(world["hass"])
    assert [r[0] for r in _rows(db)] == ["retired", "retired", "pending"]


def test_presence_detector_no_longer_runs(pa, load, tmp_path, monkeypatch):
    an = pa.PatternAnalyzer()
    db = str(tmp_path / "patterns.db")
    _db(load, db).close()
    an._db = db
    called = []
    monkeypatch.setattr(an, "_find_presence_patterns",
                        lambda conn: called.append(1) or [])
    an._detect_patterns({}, None, None, {})
    assert called == []
    assert "presence" in an._completed_types
