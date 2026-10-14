"""Fresh behaviour tests for goals.py: outcomes Nova pursues across time,
stored in the patterns database, engaged by the core tick through a headless
runner, quiet while active and announced when they finish.

Each test uses its own database file; time is injected through `now`."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

T0 = datetime(2026, 5, 9, 14, 0, 0)


def _iso(d):
    return d.isoformat(sep=" ")


@pytest.fixture
def gm(load, tmp_path, monkeypatch):
    m = load("goals")
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "patterns.db"))
    return m


@pytest.fixture
def activity(load, monkeypatch):
    db = load("database")
    rows: list = []
    monkeypatch.setattr(db, "save_activity", lambda **kw: rows.append(kw))
    return rows


def _raw(gm, gid):
    with sqlite3.connect(gm.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())


def _sql(gm, sql, *params):
    with sqlite3.connect(gm.DB_PATH) as c:
        c.execute(sql, params)


def _new(gm, **kw):
    kw.setdefault("now", T0)
    title = kw.pop("title", "Warm study")
    outcome = kw.pop("outcome", "study reaches a comfortable temperature")
    steps = kw.pop("steps", ["raise setpoint", "confirm reading"])
    return gm.create(title, outcome, steps, **kw)


# ── constants and paths ─────────────────────────────────────────────────────

def test_constants(gm):
    assert (gm.MAX_ACTIVE, gm.MAX_RUNS, gm.DEFAULT_INTERVAL_MIN) == (10, 120, 30)
    assert gm.MAX_DEADLINE_MIN == 43200 and gm.PROGRESS_TAIL == 5
    assert gm.STATUSES == ("active", "done", "failed", "cancelled")


def test_db_path_resolution(load, monkeypatch):
    m = load("goals")
    paths = load("paths")
    monkeypatch.setattr(m, "DB_PATH", None)
    assert m._db_path() == paths.patterns_db()
    monkeypatch.setattr(m, "DB_PATH", "/x/y.db")
    assert m._db_path() == "/x/y.db"


# ── create ──────────────────────────────────────────────────────────────────

def test_create_shape_and_row(gm):
    res = _new(gm)
    assert res == {
        "id": 1, "title": "Warm study",
        "outcome": "study reaches a comfortable temperature",
        "steps": [
            {"n": 1, "step": "raise setpoint", "status": "pending", "note": ""},
            {"n": 2, "step": "confirm reading", "status": "pending", "note": ""}],
        "next_check_ts": _iso(T0), "deadline_ts": None}
    row = _raw(gm, 1)
    assert row["created_ts"] == row["updated_ts"] == row["next_check_ts"] == _iso(T0)
    assert row["status"] == "active" and row["runs"] == 0
    assert row["check_interval_min"] == 30.0 and row["last_result"] == ""
    assert json.loads(row["progress"]) == []


def test_new_goal_is_due_immediately(gm):
    gid = _new(gm)["id"]
    assert [g["id"] for g in gm.due(now=T0)] == [gid]
    assert gm.due(now=T0 - timedelta(seconds=1)) == []


def test_outcome_is_required_and_stripped(gm):
    assert gm.create("t", "", []) == {"error": "empty outcome"}
    assert gm.create("t", "   ", []) == {"error": "empty outcome"}
    assert gm.create("t", None, []) == {"error": "empty outcome"}
    assert gm.create("t", "  tidy desk  ", [], now=T0)["outcome"] == "tidy desk"


def test_title_falls_back_to_outcome_prefix(gm):
    long = "o" * 80
    assert gm.create("", long, [], now=T0)["title"] == "o" * 60
    assert gm.create(None, " short one ", [], now=T0)["title"] == "short one"
    assert gm.create("  Named  ", "x", [], now=T0)["title"] == "Named"


def test_whitespace_title_does_not_fall_back(gm):
    assert gm.create("   ", "real outcome", [], now=T0)["title"] == ""


def test_steps_keep_original_numbering_and_stringify(gm):
    res = gm.create("t", "o", ["first", "", "  ", "  fourth ", 5, None], now=T0)
    assert [(s["n"], s["step"]) for s in res["steps"]] == [
        (1, "first"), (4, "fourth"), (5, "5"), (6, "None")]


def test_steps_default_to_empty(gm):
    assert gm.create("t", "o", now=T0)["steps"] == []
    assert gm.get(1)["steps"] == []


@pytest.mark.parametrize("given, stored", [
    (30, 30.0), (0, 30.0), (None, 30.0), ("abc", 30.0), (0.2, 1.0),
    (-5, 1.0), (5000, 1440.0), ("45", 45.0)])
def test_interval_clamp(gm, given, stored):
    gid = gm.create("t", "o", [], check_interval_min=given, now=T0)["id"]
    assert _raw(gm, gid)["check_interval_min"] == stored


@pytest.mark.parametrize("given, minutes", [
    (90, 90), (0, 1), (-10, 1), (10 ** 9, 43200), ("15", 15)])
def test_deadline_clamp(gm, given, minutes):
    res = gm.create("t", "o", [], deadline_minutes=given, now=T0)
    assert res["deadline_ts"] == _iso(T0 + timedelta(minutes=minutes))
    assert _raw(gm, res["id"])["deadline_ts"] == res["deadline_ts"]


def test_bad_deadline_is_ignored(gm):
    res = gm.create("t", "o", [], deadline_minutes="soonish", now=T0)
    assert "id" in res and res["deadline_ts"] is None


def test_active_cap(gm):
    for i in range(10):
        assert "id" in gm.create(f"g{i}", "o", [], now=T0)
    assert gm.create("g10", "o", [], now=T0) == {"error": "goal list is full (10 active)"}
    gm.cancel(1)
    assert "id" in gm.create("g10", "o", [], now=T0)


def test_create_on_unopenable_db(gm, tmp_path):
    res = gm.create("t", "o", [], now=T0, db_path=str(tmp_path / "nope" / "p.db"))
    assert set(res) == {"error"}


# ── reads ───────────────────────────────────────────────────────────────────

def test_get_decodes_json_and_handles_bad_json(gm):
    gid = _new(gm)["id"]
    g = gm.get(gid)
    assert g["steps"][1]["step"] == "confirm reading" and g["progress"] == []
    assert {"id", "created_ts", "updated_ts", "title", "outcome", "steps", "status",
            "progress", "next_check_ts", "check_interval_min", "deadline_ts",
            "runs", "last_result"} <= set(g)
    _sql(gm, "UPDATE goals SET steps='not json', progress='' WHERE id=?", gid)
    g = gm.get(gid)
    assert g["steps"] == [] and g["progress"] == []


def test_get_missing_and_bad_inputs(gm, tmp_path):
    _new(gm)
    assert gm.get(99) is None
    assert gm.get("x") is None
    assert gm.get("1")["id"] == 1
    assert gm.get(1, db_path=str(tmp_path / "nope" / "p.db")) is None


def test_active_and_due_order_by_next_check(gm):
    a = _new(gm, title="a")["id"]
    b = _new(gm, title="b")["id"]
    c = _new(gm, title="c")["id"]
    _sql(gm, "UPDATE goals SET next_check_ts=? WHERE id=?", _iso(T0 + timedelta(minutes=20)), a)
    _sql(gm, "UPDATE goals SET next_check_ts=? WHERE id=?", _iso(T0 + timedelta(minutes=5)), b)
    _sql(gm, "UPDATE goals SET next_check_ts=? WHERE id=?", _iso(T0 + timedelta(minutes=50)), c)
    assert [g["id"] for g in gm.active()] == [b, a, c]
    assert [g["id"] for g in gm.due(now=T0 + timedelta(minutes=20))] == [b, a]


def test_closed_goals_are_neither_active_nor_due(gm):
    a = _new(gm)["id"]
    b = _new(gm)["id"]
    gm.update(a, status="done", now=T0)
    gm.cancel(b)
    assert gm.active() == [] and gm.due(now=T0 + timedelta(days=1)) == []


def test_recent_includes_every_status_newest_first(gm):
    a = _new(gm, title="a")["id"]
    b = _new(gm, title="b")["id"]
    c = _new(gm, title="c")["id"]
    gm.update(a, status="done", now=T0 + timedelta(minutes=1))
    gm.update(c, status="failed", now=T0 + timedelta(minutes=3))
    gm.update(b, progress_note="still going", now=T0 + timedelta(minutes=2))
    assert [g["id"] for g in gm.recent()] == [c, b, a]
    assert {g["id"]: g["status"] for g in gm.recent()} == {a: "done", b: "active", c: "failed"}


def test_recent_limit_clamp(gm):
    for i in range(4):
        _new(gm, title=str(i))
    assert len(gm.recent(limit=2)) == 2
    assert len(gm.recent(limit=0)) == 1
    assert len(gm.recent(limit=-3)) == 1
    assert len(gm.recent(limit=500)) == 4
    assert gm.recent(limit="many") == []


def test_reads_on_unopenable_db(gm, tmp_path):
    bad = str(tmp_path / "nope" / "p.db")
    assert gm.active(db_path=bad) == []
    assert gm.due(now=T0, db_path=bad) == []
    assert gm.recent(db_path=bad) == []


# ── update ──────────────────────────────────────────────────────────────────

def test_update_missing_goal(gm):
    assert gm.update(42, progress_note="x") == {"error": "no goal #42"}


def test_update_steps(gm):
    gid = _new(gm)["id"]
    res = gm.update(gid, step_updates=[
        {"n": 1, "status": "done", "note": "n" * 400},
        {"n": "2", "status": "bogus", "note": "seen"},
        {"n": 7, "status": "done"},
        {"status": "failed"},
    ], now=T0)
    assert res == {"ok": True, "id": gid}
    s1, s2 = gm.get(gid)["steps"]
    assert s1["status"] == "done" and s1["note"] == "n" * 300
    assert s2["status"] == "pending" and s2["note"] == "seen"


@pytest.mark.parametrize("status", ["pending", "done", "failed", "skipped"])
def test_every_step_status_is_accepted(gm, status):
    gid = _new(gm)["id"]
    gm.update(gid, step_updates=[{"n": 1, "status": status}], now=T0)
    assert gm.get(gid)["steps"][0]["status"] == status


def test_empty_note_does_not_clear_existing(gm):
    gid = _new(gm)["id"]
    gm.update(gid, step_updates=[{"n": 1, "note": "keep"}], now=T0)
    gm.update(gid, step_updates=[{"n": 1, "note": ""}], now=T0)
    assert gm.get(gid)["steps"][0]["note"] == "keep"


def test_bad_step_update_discards_the_whole_call(gm):
    gid = _new(gm)["id"]
    res = gm.update(gid, step_updates=[{"n": "one"}], status="done",
                    progress_note="x", now=T0)
    assert set(res) == {"error"}
    g = gm.get(gid)
    assert g["status"] == "active" and g["progress"] == []
    assert set(gm.update(gid, step_updates=["not a dict"], now=T0)) == {"error"}


def test_progress_log_and_trim(gm):
    gid = _new(gm)["id"]
    gm.update(gid, progress_note="p" * 600, now=T0)
    g = gm.get(gid)
    assert g["progress"] == [{"t": _iso(T0), "note": "p" * 500}]
    for i in range(60):
        gm.update(gid, progress_note=f"note {i}", now=T0 + timedelta(minutes=i))
    log = gm.get(gid)["progress"]
    assert len(log) == 50 and log[-1]["note"] == "note 59" and log[0]["note"] == "note 10"


def test_update_always_stamps_updated_ts(gm):
    gid = _new(gm)["id"]
    later = T0 + timedelta(hours=2)
    gm.update(gid, now=later)
    assert _raw(gm, gid)["updated_ts"] == _iso(later)


@pytest.mark.parametrize("given, minutes", [(45, 45), (0, 1), (-2, 1), (99999, 10080), ("20", 20)])
def test_next_check_clamp(gm, given, minutes):
    gid = _new(gm)["id"]
    gm.update(gid, next_check_minutes=given, now=T0)
    assert _raw(gm, gid)["next_check_ts"] == _iso(T0 + timedelta(minutes=minutes))


def test_bad_next_check_is_skipped_but_rest_applies(gm):
    gid = _new(gm)["id"]
    assert gm.update(gid, next_check_minutes="later", progress_note="n", now=T0)["ok"]
    row = _raw(gm, gid)
    assert row["next_check_ts"] == _iso(T0) and json.loads(row["progress"])[0]["note"] == "n"


def test_status_values(gm):
    gid = _new(gm)["id"]
    gm.update(gid, status="paused", now=T0)
    assert gm.get(gid)["status"] == "active"
    gm.update(gid, status="skipped", now=T0)
    assert gm.get(gid)["status"] == "active"
    gm.update(gid, status="done", now=T0)
    assert gm.get(gid)["status"] == "done"
    gm.update(gid, status="active", now=T0)              # can re-open
    assert gm.get(gid)["status"] == "active"


def test_result_written_truncated_and_clearable(gm):
    gid = _new(gm)["id"]
    gm.update(gid, result="r" * 1500, now=T0)
    assert gm.get(gid)["last_result"] == "r" * 1000
    gm.update(gid, now=T0)
    assert gm.get(gid)["last_result"] == "r" * 1000      # None leaves it
    gm.update(gid, result="", now=T0)
    assert gm.get(gid)["last_result"] == ""
    gm.update(gid, result=12, now=T0)
    assert gm.get(gid)["last_result"] == "12"


# ── cancel / delete / arm ───────────────────────────────────────────────────

def test_cancel_only_active(gm):
    gid = _new(gm)["id"]
    assert gm.cancel(gid) is True
    row = _raw(gm, gid)
    assert row["status"] == "cancelled"
    assert row["updated_ts"] != _iso(T0)                  # wall clock, not T0
    assert gm.cancel(gid) is False
    other = _new(gm)["id"]
    gm.update(other, status="done", now=T0)
    assert gm.cancel(other) is False
    assert gm.cancel(999) is False
    assert gm.cancel("bad") is False


def test_delete_any_status(gm, tmp_path):
    a = _new(gm)["id"]
    b = _new(gm)["id"]
    gm.cancel(b)
    assert gm.delete(a) is True and gm.get(a) is None
    assert gm.delete(b) is True and gm.get(b) is None
    assert gm.delete(a) is False
    assert gm.delete(1, db_path=str(tmp_path / "nope" / "p.db")) is False


def test_arm_sets_next_check_and_runs(gm, tmp_path):
    gid = _new(gm)["id"]
    when = T0 + timedelta(minutes=17)
    assert gm._arm(gid, when, 4) is None
    row = _raw(gm, gid)
    assert row["next_check_ts"] == _iso(when) and row["runs"] == 4
    gm._arm(gid, when, 5, db_path=str(tmp_path / "nope" / "p.db"))   # swallowed


# ── build_prompt ────────────────────────────────────────────────────────────

def _goal(**kw):
    g = {"id": 7, "title": "Air out loft", "outcome": "loft humidity below 60%",
         "steps": [], "progress": []}
    g.update(kw)
    return g


def test_prompt_minimal(gm):
    lines = gm.build_prompt(_goal()).split("\n")
    assert lines[0] == "You are working on YOUR OWN goal #7: Air out loft"
    assert lines[1] == "OUTCOME to achieve: loft humidity below 60%"
    assert len(lines) == 3
    tail = lines[2]
    assert "update_goal(goal_id=7, ...)" in tail and "exactly once" in tail
    assert "cannot control" in tail and "next_check_minutes" in tail
    assert "status='done'/'failed'" in tail and "progress_note" in tail


def test_prompt_plan_marks_and_notes(gm):
    steps = [{"n": 1, "step": "open vent", "status": "done", "note": "opened"},
             {"n": 2, "step": "run fan", "status": "pending", "note": ""},
             {"n": 3, "step": "check sensor", "status": "failed", "note": "offline"},
             {"n": 4, "step": "close vent", "status": "skipped", "note": ""},
             {"n": 5, "step": "odd", "status": "weird", "note": ""}]
    p = gm.build_prompt(_goal(steps=steps)).split("\n")
    i = p.index("Plan:")
    assert p[i + 1:i + 6] == [
        "  [x] 1. open vent — opened",
        "  [ ] 2. run fan",
        "  [!] 3. check sensor — offline",
        "  [-] 4. close vent",
        "  [ ] 5. odd"]


def test_prompt_progress_tail(gm):
    progress = [{"t": f"t{i}", "note": f"n{i}"} for i in range(8)]
    p = gm.build_prompt(_goal(progress=progress)).split("\n")
    i = p.index("Recent progress:")
    assert p[i + 1:i + 6] == [f"  t{k}: n{k}" for k in range(3, 8)]
    assert "  t2: n2" not in p


def test_prompt_deadline_lines(gm):
    p = gm.build_prompt(_goal(deadline_ts="2026-05-10 09:00:00"))
    assert "Deadline: 2026-05-10 09:00:00" in p.split("\n")
    assert "DEADLINE HAS PASSED" not in p
    late = gm.build_prompt(_goal(), deadline_passed=True).split("\n")
    assert late[2].startswith("THE DEADLINE HAS PASSED")
    assert not any(line.startswith("Deadline:") for line in late)


# ── finish action ───────────────────────────────────────────────────────────

def test_finish_action_variants(gm):
    base = {"type": "goal", "urgency": "medium", "auto_act": True}
    assert gm._finish_action({"title": "X", "status": "done", "last_result": "All set."}) == {
        **base, "message": "All set."}
    assert gm._finish_action({"title": "X", "status": "done", "last_result": ""})["message"] == \
        "Goal 'X' is complete."
    assert gm._finish_action({"title": "X", "status": "failed"})["message"] == "Goal 'X' is closed."
    assert gm._finish_action(None)["message"] == "Goal '' is closed."


# ── async_process_due ───────────────────────────────────────────────────────

async def test_nothing_due_or_no_runner(gm, fake_hass):
    gid = _new(gm)["id"]
    assert await gm.async_process_due(fake_hass, {}, None, now=T0) == []
    row = _raw(gm, gid)
    assert row["runs"] == 0 and row["status"] == "active"      # untouched

    async def runner(prompt, ctx):
        raise AssertionError("nothing due")
    assert await gm.async_process_due(fake_hass, {}, runner, now=T0 - timedelta(hours=1)) == []


async def test_quiet_run_arms_counts_and_logs(gm, fake_hass, activity):
    gid = _new(gm, check_interval_min=20)["id"]
    seen = []

    async def runner(prompt, ctx):
        seen.append((prompt, ctx))
        return "  looked at the thermostat  "

    actions = await gm.async_process_due(fake_hass, {}, runner, now=T0)
    assert actions == []
    assert seen[0][1] == "" and seen[0][0] == gm.build_prompt(gm.get(gid) | {
        "runs": 0, "next_check_ts": _iso(T0)})
    row = _raw(gm, gid)
    assert row["runs"] == 1 and row["status"] == "active"
    assert row["next_check_ts"] == _iso(T0 + timedelta(minutes=20))
    assert activity == [{"category": "goal", "urgency": "low",
                         "message": f"Goal #{gid} 'Warm study' engaged (run 1): looked at the thermostat",
                         "source": "agent"}]


async def test_runner_can_override_the_pre_armed_check(gm, fake_hass, activity):
    gid = _new(gm)["id"]

    async def runner(prompt, ctx):
        gm.update(gid, next_check_minutes=90, now=T0)
        return ""

    await gm.async_process_due(fake_hass, {}, runner, now=T0)
    assert _raw(gm, gid)["next_check_ts"] == _iso(T0 + timedelta(minutes=90))
    assert activity[0]["message"].endswith("(run 1): no report")


async def test_activity_message_truncates_result(gm, fake_hass, activity):
    _new(gm)

    async def runner(prompt, ctx):
        return "w" * 400

    await gm.async_process_due(fake_hass, {}, runner, now=T0)
    assert activity[0]["message"].endswith(": " + "w" * 160)


async def test_finished_goal_is_announced(gm, fake_hass, activity):
    gid = _new(gm)["id"]

    async def runner(prompt, ctx):
        gm.update(gid, status="done", result="The study is warm now.", now=T0)
        return "closed"

    actions = await gm.async_process_due(fake_hass, {}, runner, now=T0)
    assert actions == [{"type": "goal", "urgency": "medium",
                        "message": "The study is warm now.", "auto_act": True}]


async def test_failed_goal_without_result_gets_default_message(gm, fake_hass, activity):
    gid = _new(gm)["id"]

    async def runner(prompt, ctx):
        gm.update(gid, status="failed", now=T0)
        return ""

    actions = await gm.async_process_due(fake_hass, {}, runner, now=T0)
    assert actions[0]["message"] == "Goal 'Warm study' is closed."


async def test_cancelled_during_run_is_quiet(gm, fake_hass, activity):
    gid = _new(gm)["id"]

    async def runner(prompt, ctx):
        gm.cancel(gid)
        return "never mind"

    assert await gm.async_process_due(fake_hass, {}, runner, now=T0) == []
    assert len(activity) == 1


async def test_deleted_during_run_is_skipped(gm, fake_hass, activity):
    gid = _new(gm)["id"]

    async def runner(prompt, ctx):
        gm.delete(gid)
        return "gone"

    assert await gm.async_process_due(fake_hass, {}, runner, now=T0) == []
    assert activity == []


async def test_runner_error_keeps_goal_active_and_armed(gm, fake_hass, activity):
    gid = _new(gm)["id"]

    async def runner(prompt, ctx):
        raise RuntimeError("model unavailable")

    assert await gm.async_process_due(fake_hass, {}, runner, now=T0) == []
    g = gm.get(gid)
    assert g["status"] == "active" and g["runs"] == 1
    assert g["progress"][-1] == {"t": _iso(T0), "note": "run error: model unavailable"}
    assert g["next_check_ts"] == _iso(T0 + timedelta(minutes=30))
    assert activity == []


async def test_deadline_passed_forces_closure(gm, fake_hass, activity):
    gid = _new(gm, deadline_minutes=60)["id"]
    later = T0 + timedelta(minutes=61)
    _sql(gm, "UPDATE goals SET next_check_ts=? WHERE id=?", _iso(later), gid)
    prompts = []

    async def runner(prompt, ctx):
        prompts.append(prompt)
        return "Could not finish in time."

    actions = await gm.async_process_due(fake_hass, {}, runner, now=later)
    assert "DEADLINE HAS PASSED" in prompts[0]
    g = gm.get(gid)
    assert g["status"] == "failed" and g["last_result"] == "Could not finish in time."
    assert g["progress"][-1]["note"] == "deadline passed — force-closed"
    assert actions == [{"type": "goal", "urgency": "medium",
                        "message": "Could not finish in time.", "auto_act": True}]


async def test_deadline_force_close_without_report(gm, fake_hass, activity):
    gid = _new(gm, title="Prep", deadline_minutes=5)["id"]
    later = T0 + timedelta(minutes=6)

    async def runner(prompt, ctx):
        return ""

    actions = await gm.async_process_due(fake_hass, {}, runner, now=later)
    assert actions[0]["message"] == "The deadline for 'Prep' passed."
    assert gm.get(gid)["status"] == "failed"


async def test_deadline_exactly_now_is_not_passed(gm, fake_hass, activity):
    gid = _new(gm, deadline_minutes=10)["id"]
    at = T0 + timedelta(minutes=10)
    prompts = []

    async def runner(prompt, ctx):
        prompts.append(prompt)
        return ""

    await gm.async_process_due(fake_hass, {}, runner, now=at)
    assert "DEADLINE HAS PASSED" not in prompts[0]
    assert gm.get(gid)["status"] == "active"


async def test_model_closing_after_deadline_is_respected(gm, fake_hass, activity):
    gid = _new(gm, deadline_minutes=1)["id"]
    later = T0 + timedelta(minutes=2)

    async def runner(prompt, ctx):
        gm.update(gid, status="done", result="Made it after all.", now=later)
        return "x"

    actions = await gm.async_process_due(fake_hass, {}, runner, now=later)
    assert gm.get(gid)["status"] == "done"
    assert actions[0]["message"] == "Made it after all."


async def test_run_budget_exhaustion(gm, fake_hass, activity):
    gid = _new(gm, title="Endless")["id"]
    _sql(gm, "UPDATE goals SET runs=? WHERE id=?", gm.MAX_RUNS, gid)

    async def runner(prompt, ctx):
        raise AssertionError("must not run")

    actions = await gm.async_process_due(fake_hass, {}, runner, now=T0)
    g = gm.get(gid)
    assert g["status"] == "failed" and g["runs"] == gm.MAX_RUNS + 1
    expected = ("I've engaged this goal 120 times without completing it, "
                "so I'm setting it aside: Endless")
    assert g["last_result"] == expected
    assert g["progress"][-1]["note"] == "run budget exhausted"
    assert actions == [{"type": "goal", "urgency": "medium", "message": expected,
                        "auto_act": True}]
    assert activity == []


async def test_last_run_within_budget_still_runs(gm, fake_hass, activity):
    gid = _new(gm)["id"]
    _sql(gm, "UPDATE goals SET runs=? WHERE id=?", gm.MAX_RUNS - 1, gid)
    ran = []

    async def runner(prompt, ctx):
        ran.append(1)
        return ""

    await gm.async_process_due(fake_hass, {}, runner, now=T0)
    assert ran and gm.get(gid)["runs"] == gm.MAX_RUNS


async def test_several_goals_in_due_order(gm, fake_hass, activity):
    a = _new(gm, title="A")["id"]
    b = _new(gm, title="B")["id"]
    _sql(gm, "UPDATE goals SET next_check_ts=? WHERE id=?", _iso(T0 - timedelta(minutes=5)), b)
    order = []

    async def runner(prompt, ctx):
        order.append(prompt.split("\n")[0])
        if "#%d:" % a in prompt:
            gm.update(a, status="done", result="A done", now=T0)
        return ""

    actions = await gm.async_process_due(fake_hass, {}, runner, now=T0)
    assert order == [f"You are working on YOUR OWN goal #{b}: B",
                     f"You are working on YOUR OWN goal #{a}: A"]
    assert [x["message"] for x in actions] == ["A done"]


async def test_loading_failure_returns_empty(gm, fake_hass, monkeypatch):
    async def broken(func, *a):
        raise RuntimeError("executor down")
    monkeypatch.setattr(fake_hass, "async_add_executor_job", broken)

    async def runner(prompt, ctx):
        raise AssertionError

    assert await gm.async_process_due(fake_hass, {}, runner, now=T0) == []


async def test_activity_failure_is_swallowed(gm, fake_hass, load, monkeypatch):
    db = load("database")

    def boom(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(db, "save_activity", boom)
    gid = _new(gm)["id"]

    async def runner(prompt, ctx):
        gm.update(gid, status="done", result="ok", now=T0)
        return ""

    assert len(await gm.async_process_due(fake_hass, {}, runner, now=T0)) == 1


async def test_explicit_db_path(gm, fake_hass, activity, tmp_path):
    other = str(tmp_path / "side.db")
    gid = gm.create("t", "o", [], now=T0, db_path=other)["id"]

    async def runner(prompt, ctx):
        return ""

    await gm.async_process_due(fake_hass, {}, runner=runner, now=T0, db_path=other)
    assert gm.get(gid, db_path=other)["runs"] == 1
    assert gm.get(gid) is None
