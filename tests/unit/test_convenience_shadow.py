"""8.48.0 (design D2, step 7): the convenience path shadows anticipation alerts.

* Only anticipation alerts are on it. Safety and security alerts (a lock,
  cover or alarm, the "less secure than usual" alert, anything critical)
  never are, so learned stopping can never reach them.
* Both paths judge the same alert from the same reading. The new path's
  verdict sends nothing; the current path still delivers, once.
* Each pair is kept with its reasons and the reading's id.
* After two Not helpful phone ratings in a row on one pattern the panel
  asks whether to stop it. Only that pattern stops, only on your yes, and
  you can restart it.
* The new path can decide a pattern only after 14 days and 10 comparisons,
  only when you switch it on, and you can switch it back.

All state is fake. Nothing touches a real speaker or phone.
"""
import ast
import pathlib
import time

import pytest

SPEAKER = "media_player.hall"
DAY = 86400.0


def _alert(**over):
    a = {"type": "anticipation_overdue", "urgency": "low",
         "message": "The back door usually has activity by around 07:45.",
         "pattern_key": "overdue:binary_sensor.back_door", "offer": False,
         "decision_id": None}
    a.update(over)
    return a


@pytest.fixture
def path(load, fake_hass, monkeypatch):
    cv, cd, dl, dr = (load("convenience"), load("core_delivery"), load("delivery"),
                      load("decision_record"))
    hab, og, tts = load("habituation"), load("output_gate"), load("tts_helper")
    out = {"spoken": [], "pushed": [], "readings": []}
    monkeypatch.setattr(hab, "is_quiet", lambda k: False)
    monkeypatch.setattr(hab, "record", lambda *a, **k: None)
    monkeypatch.setattr(og, "_STATE", og.GateState())
    monkeypatch.setattr(load("database"), "save_activity", lambda **kw: None)

    async def _push(hass, config, message, action_type, snap=None, **kw):
        out["pushed"].append(message)
        return ["notify.phone"]

    async def _announce(hass, message, tts_entity, targets, **kw):
        out["spoken"].append(message)
        return True
    monkeypatch.setattr(cd, "_push_notification", _push)
    monkeypatch.setattr(tts, "async_announce", _announce)
    monkeypatch.setattr(tts, "resolve_tts_for_context", lambda *a, **k: "tts.x")
    monkeypatch.setattr(dl, "_announcement_speakers", lambda hass: [SPEAKER])
    out["route"] = ([SPEAKER], "announce")
    monkeypatch.setattr(dl, "_speakers", lambda hass, req, cond: out["route"])
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "_in_quiet_hours", lambda *a: False)
    monkeypatch.setattr(sd, "is_sleeping", lambda *a, **k: (False, "awake"))
    fake_hass.states.set(SPEAKER, "idle")
    fake_hass.states.set("person.a", "home")
    out.update(cv=cv, cd=cd, dl=dl, dr=dr, og=og)

    real_emit = cd._emit_action

    async def _emit(hass, config, action, sleeping, *, reading=None):
        out["readings"].append(("current", reading))
        return await real_emit(hass, config, action, sleeping, reading=reading)
    monkeypatch.setattr(cd, "_emit_action", _emit)
    real_decide = cv.decide

    def _decide(hass, config, action, sleeping, reading):
        out["readings"].append(("new", reading))
        return real_decide(hass, config, action, sleeping, reading)
    monkeypatch.setattr(cv, "decide", _decide)

    async def handle(action, *, sleeping=False):
        ap = load("alert_path")
        reading = ap.situation(fake_hass, {}, sleeping=sleeping, quiet=False)
        await cv.async_handle(fake_hass, {"broadcast_group": SPEAKER}, action, sleeping,
                              reading)
        return reading
    out["handle"] = handle
    return out


def _rows(cv):
    return cv.status()["recent"]


# ── what is on the path ─────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["anticipation_overdue", "anticipation_routine",
                                  "anticipation_departure", "anticipation_presence",
                                  "anticipation_arriving"])
def test_anticipation_alerts_are_on_the_path(load, kind):
    assert load("convenience").eligible(_alert(type=kind))


@pytest.mark.parametrize("over", [
    {"type": "anticipation", "entity_id": "binary_sensor.back_door"},   # less secure
    {"entity_id": "lock.back_door"},
    {"entity_id": "cover.garage_door"},
    {"entity_id": "alarm_control_panel.house"},
    {"urgency": "critical"},
    {"type": "intrusion_investigating"},
    {"type": "critical_hazard"},
    {"type": "proactive_offer"},
    {"pattern_key": ""},
])
def test_safety_security_and_everything_else_stay_off_it(load, over):
    assert not load("convenience").eligible(_alert(**over))


# ── shadow ──────────────────────────────────────────────────────────────────

async def test_the_shadow_sends_nothing_and_the_current_path_delivers_once(path):
    reading = await path["handle"](_alert())
    assert path["spoken"] == ["The back door usually has activity by around 07:45."]
    assert path["pushed"] == []
    (row,) = _rows(path["cv"])
    assert (row["actual"], row["shadow"]) == ("said", "said")
    assert row["decided_by"] == "current"
    assert row["snapshot_id"] == reading.snapshot_id
    assert row["actual_reason"] and row["shadow_reason"]


async def test_both_paths_judge_the_same_reading(path):
    reading = await path["handle"](_alert())
    assert [w for w, _ in path["readings"]] == ["new", "current"]   # shadow first
    assert all(r is reading for _, r in path["readings"])


async def test_a_held_alert_is_held_by_both_with_their_reasons(path, fake_hass):
    fake_hass.states.set("person.a", "home")
    await path["handle"](_alert(), sleeping=True)
    assert path["spoken"] == [] and path["pushed"] == []
    (row,) = _rows(path["cv"])
    assert (row["actual"], row["shadow"]) == ("held", "held")
    assert row["actual_reason"] == row["shadow_reason"]


async def test_a_difference_is_kept_and_shown(path, monkeypatch):
    cv = path["cv"]
    monkeypatch.setattr(cv, "decide", lambda *a: cv.Verdict(False, "test hold"))
    await path["handle"](_alert())
    assert len(path["spoken"]) == 1                 # the current path still decides
    (row,) = _rows(cv)
    assert (row["actual"], row["shadow"]) == ("said", "held")
    assert row["shadow_reason"] == "test hold"
    (p,) = cv.status()["patterns"]
    assert (p["decisions"], p["agree"]) == (1, 0)


async def test_a_shadow_that_fails_never_stops_the_alert(path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad reading")
    monkeypatch.setattr(path["cv"], "decide", boom)
    await path["handle"](_alert())
    assert len(path["spoken"]) == 1
    (row,) = _rows(path["cv"])
    assert row["shadow_reason"] == "the new path could not decide"


def test_the_tick_sends_anticipation_alerts_through_the_path():
    src = (pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
           / "core_runtime.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_tick")
    body = ast.unparse(fn)
    assert "convenience.eligible(action)" in body
    assert "convenience.async_handle(hass, config, action, sleeping, reading)" in body


# ── stopping one pattern, on your yes ───────────────────────────────────────

def _rated(path, verdicts, *, key="overdue:binary_sensor.back_door", source="phone",
           start=None):
    """Alerts of one pattern, each rated in turn."""
    cv, dr = path["cv"], path["dr"]
    t = start or time.time() - 3600
    for i, v in enumerate(verdicts):
        rid = dr.record("anticipation_overdue", decision="flag overdue activity")
        cv.record(key, "anticipation_overdue", "s", rid, "m", cv.Verdict(True, "spoken"),
                  cv.Verdict(True, "spoken"), cv.CURRENT, now=t + i)
        if v:
            dr.set_outcome(rid, v, source, ts=t + i + 0.5)


def _ask(cv, key="overdue:binary_sensor.back_door"):
    return next(p for p in cv.status()["patterns"] if p["pattern_key"] == key)["ask_stop"]


@pytest.mark.parametrize("verdicts,asks", [
    (["unnecessary", "unnecessary"], True),
    (["good", "unnecessary", "wrong"], True),
    (["unnecessary"], False),
    (["unnecessary", "good"], False),
    (["unnecessary", None, "good"], False),
])
def test_two_not_helpful_in_a_row_asks(path, verdicts, asks):
    _rated(path, verdicts)
    assert _ask(path["cv"]) is asks


def test_only_your_phone_ratings_count(path):
    _rated(path, ["unnecessary", "unnecessary"], source="panel")
    assert _ask(path["cv"]) is False


def test_the_question_is_for_that_pattern_only(path):
    _rated(path, ["unnecessary", "unnecessary"])
    _rated(path, ["unnecessary"], key="routine:person.a:x")
    cv = path["cv"]
    assert _ask(cv) is True and _ask(cv, "routine:person.a:x") is False


def test_keep_it_waits_for_two_new_ratings(path):
    cv = path["cv"]
    _rated(path, ["unnecessary", "unnecessary"])
    assert cv.set_choice("overdue:binary_sensor.back_door", "keep") == "ok"
    assert _ask(cv) is False
    _rated(path, ["unnecessary"], start=time.time() + 10)
    assert _ask(cv) is False
    _rated(path, ["unnecessary"], start=time.time() + 20)
    assert _ask(cv) is True


async def test_a_stopped_pattern_says_nothing_and_can_restart(path, monkeypatch):
    cv, key = path["cv"], "overdue:binary_sensor.back_door"
    assert cv.set_choice(key, "stop") == "ok"
    await path["handle"](_alert())
    assert path["spoken"] == [] and path["pushed"] == []
    (row,) = _rows(cv)
    assert row["actual_reason"] == row["shadow_reason"] == "you stopped this pattern"
    assert row["decided_by"] == "stopped"
    other = _alert(pattern_key="overdue:binary_sensor.side_door",
                   message="The side door usually has activity by now.")
    await path["handle"](other)                     # the same kind is not stopped
    assert path["spoken"] == ["The side door usually has activity by now."]
    assert cv.set_choice(key, "restart") == "ok"
    monkeypatch.setattr(path["og"], "_STATE", path["og"].GateState())
    await path["handle"](_alert())
    assert len(path["spoken"]) == 2


def test_a_stopped_pattern_is_not_asked_about(path):
    _rated(path, ["unnecessary", "unnecessary"])
    path["cv"].set_choice("overdue:binary_sensor.back_door", "stop")
    assert _ask(path["cv"]) is False


def test_a_restart_forgets_the_ratings_before_it(path):
    cv = path["cv"]
    _rated(path, ["unnecessary", "unnecessary"])
    cv.set_choice("overdue:binary_sensor.back_door", "stop")
    cv.set_choice("overdue:binary_sensor.back_door", "restart")
    assert _ask(cv) is False


# ── switching a pattern over, on your approval ──────────────────────────────

def _compared(cv, n, days, key="overdue:binary_sensor.back_door"):
    start = time.time() - days * DAY
    for i in range(n):
        cv.record(key, "anticipation_overdue", "s", None, "m", cv.Verdict(True, "spoken"),
                  cv.Verdict(True, "spoken"), cv.CURRENT, now=start + i)


@pytest.mark.parametrize("n,days,ok", [(10, 14.1, True), (9, 20, False), (30, 13.9, False)])
def test_switching_over_needs_14_days_and_10_comparisons(path, n, days, ok):
    cv = path["cv"]
    _compared(cv, n, days)
    (p,) = cv.status()["patterns"]
    assert p["ready"] is ok
    assert cv.set_choice("overdue:binary_sensor.back_door", "activate") == (
        "ok" if ok else "not_ready")


def test_nothing_switches_over_on_its_own(path):
    cv = path["cv"]
    _compared(cv, 40, 30)
    assert cv.choice("overdue:binary_sensor.back_door") == (False, False)


async def test_a_switched_pattern_follows_the_new_path_and_switches_back(path, monkeypatch):
    cv, key = path["cv"], "overdue:binary_sensor.back_door"
    _compared(cv, 10, 15)
    assert cv.set_choice(key, "activate") == "ok"
    monkeypatch.setattr(cv, "decide", lambda *a: cv.Verdict(False, "test hold"))
    await path["handle"](_alert())
    assert path["spoken"] == [] and path["pushed"] == []
    assert _rows(cv)[0]["decided_by"] == "new"
    monkeypatch.setattr(cv, "decide", lambda *a: cv.Verdict(True, "spoken"))
    og = path["og"]
    monkeypatch.setattr(og, "_STATE", og.GateState())    # not a repeat
    await path["handle"](_alert())
    assert len(path["spoken"]) == 1 and _rows(cv)[0]["decided_by"] == "new"
    assert cv.set_choice(key, "deactivate") == "ok"
    monkeypatch.setattr(cv, "decide", lambda *a: cv.Verdict(False, "test hold"))
    monkeypatch.setattr(og, "_STATE", og.GateState())
    await path["handle"](_alert())
    assert len(path["spoken"]) == 2 and _rows(cv)[0]["decided_by"] == "current"


def test_an_unknown_choice_changes_nothing(path):
    assert path["cv"].set_choice("overdue:x", "mute_category") == "unknown"


def test_old_rows_are_pruned(path):
    cv = path["cv"]
    cv.record("k", "anticipation_overdue", "s", None, "m", cv.Verdict(True), cv.Verdict(True),
              cv.CURRENT, now=time.time() - (cv.KEEP_DAYS + 1) * DAY)
    cv.record("k", "anticipation_overdue", "s", None, "m", cv.Verdict(True), cv.Verdict(True),
              cv.CURRENT)
    assert len(_rows(cv)) == 1
