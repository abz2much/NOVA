"""Fresh behaviour tests for followups.py: the agent's self-scheduled
instructions, stored in the patterns database and executed by the core tick
through a headless runner.

Each test points the module at its own database file. Time is injected
through the `now` arguments so nothing depends on the wall clock."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

T0 = datetime(2026, 3, 4, 8, 15, 0)


@pytest.fixture
def fu(load, tmp_path, monkeypatch):
    m = load("followups")
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "patterns.db"))
    return m


@pytest.fixture
def activity(load, monkeypatch):
    db = load("database")
    rows: list = []
    monkeypatch.setattr(db, "save_activity", lambda **kw: rows.append(kw))
    return rows


def _rows(fu):
    with sqlite3.connect(fu.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM followups ORDER BY id")]


def _iso(d):
    return d.isoformat(sep=" ")


# ── path resolution ─────────────────────────────────────────────────────────

def test_db_path_override_and_default(load, monkeypatch):
    m = load("followups")
    paths = load("paths")
    monkeypatch.setattr(m, "DB_PATH", None)
    assert m._db_path() == paths.patterns_db()
    monkeypatch.setattr(m, "DB_PATH", "/somewhere/else.db")
    assert m._db_path() == "/somewhere/else.db"


def test_constants(fu):
    assert fu.MAX_OPEN == 25
    assert fu.MAX_DELAY_MINUTES == 10080
    assert fu.STATUSES == ("pending", "done", "cancelled", "failed")


# ── schedule ────────────────────────────────────────────────────────────────

def test_schedule_returns_id_due_and_stripped_instruction(fu):
    res = fu.schedule("  look at the porch light  ", 10, now=T0)
    assert res == {"id": 1, "due_ts": _iso(T0 + timedelta(minutes=10)),
                   "instruction": "look at the porch light"}
    row = _rows(fu)[0]
    assert row["created_ts"] == _iso(T0)
    assert row["due_ts"] == res["due_ts"]
    assert row["status"] == "pending" and row["result"] == ""
    assert row["context"] == ""


def test_schedule_keeps_context_verbatim(fu):
    fu.schedule("x", 1, context="  user asked at dinner ", now=T0)
    assert _rows(fu)[0]["context"] == "  user asked at dinner "


def test_ids_increase(fu):
    a = fu.schedule("a", 1, now=T0)["id"]
    b = fu.schedule("b", 1, now=T0)["id"]
    assert b == a + 1


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_instruction_is_an_error(fu, text):
    assert fu.schedule(text, 5, now=T0) == {"error": "empty instruction"}
    assert fu.pending() == []


def test_empty_instruction_is_checked_before_the_delay(fu):
    assert fu.schedule("", "nonsense") == {"error": "empty instruction"}


@pytest.mark.parametrize("bad, shown", [("soon", "'soon'"), (None, "None"), ([], "[]")])
def test_unparseable_delay_is_an_error(fu, bad, shown):
    assert fu.schedule("x", bad, now=T0) == {"error": f"bad delay: {shown}"}
    assert fu.pending() == []


@pytest.mark.parametrize("delay, minutes", [
    (0, 0.1), (-30, 0.1), (0.05, 0.1), ("2.5", 2.5),
    (10080, 10080), (99999, 10080), (float("inf"), 10080)])
def test_delay_is_clamped(fu, delay, minutes):
    res = fu.schedule("x", delay, now=T0)
    assert res["due_ts"] == _iso(T0 + timedelta(minutes=minutes))


def test_queue_cap_counts_only_pending(fu, monkeypatch):
    monkeypatch.setattr(fu, "MAX_OPEN", 2)
    a = fu.schedule("a", 1, now=T0)["id"]
    fu.schedule("b", 1, now=T0)
    full = fu.schedule("c", 1, now=T0)
    assert full == {"error": "follow-up queue is full (2 pending)"}
    assert len(_rows(fu)) == 2
    fu.cancel(a)                                  # frees a slot
    assert "id" in fu.schedule("c", 1, now=T0)


def test_default_cap_is_twenty_five(fu):
    for i in range(25):
        assert "id" in fu.schedule(f"task {i}", 1, now=T0)
    assert fu.schedule("one more", 1, now=T0)["error"].startswith("follow-up queue is full")


def test_unopenable_database_gives_error_dict(fu, tmp_path):
    res = fu.schedule("x", 1, now=T0, db_path=str(tmp_path / "missing" / "p.db"))
    assert set(res) == {"error"} and res["error"]
    assert not (tmp_path / "missing").exists()     # never creates the folder


def test_explicit_db_path_wins(fu, tmp_path):
    other = str(tmp_path / "other.db")
    fu.schedule("elsewhere", 1, now=T0, db_path=other)
    assert fu.pending(db_path=other)[0]["instruction"] == "elsewhere"
    assert fu.pending() == []


# ── pending ─────────────────────────────────────────────────────────────────

def test_pending_lists_everything_open_sorted_by_due(fu):
    fu.schedule("late", 60, now=T0)
    fu.schedule("early", 5, now=T0)
    fu.schedule("middle", 30, now=T0)
    rows = fu.pending(now=T0)
    assert [r["instruction"] for r in rows] == ["early", "middle", "late"]
    assert set(rows[0]) == {"id", "created_ts", "due_ts", "instruction",
                            "context", "status", "result"}


def test_pending_without_due_only_ignores_now(fu):
    fu.schedule("far future", 600, now=T0)
    assert len(fu.pending(now=T0 - timedelta(days=1))) == 1


def test_due_only_uses_inclusive_cutoff(fu):
    fu.schedule("five", 5, now=T0)
    fu.schedule("ten", 10, now=T0)
    assert fu.pending(now=T0 + timedelta(minutes=4), due_only=True) == []
    exactly = fu.pending(now=T0 + timedelta(minutes=5), due_only=True)
    assert [r["instruction"] for r in exactly] == ["five"]
    both = fu.pending(now=T0 + timedelta(minutes=11), due_only=True)
    assert [r["instruction"] for r in both] == ["five", "ten"]


def test_due_only_compares_whole_seconds(fu):
    fu.schedule("x", 1, now=T0 + timedelta(microseconds=700000))
    # due 08:16:00.7, but SQLite datetime() drops the fraction
    assert len(fu.pending(now=T0 + timedelta(minutes=1), due_only=True)) == 1


def test_pending_excludes_closed_rows(fu):
    a = fu.schedule("a", 1, now=T0)["id"]
    b = fu.schedule("b", 1, now=T0)["id"]
    fu.schedule("c", 1, now=T0)
    fu.cancel(a)
    fu.mark(b, "done", "ok")
    assert [r["instruction"] for r in fu.pending()] == ["c"]


def test_pending_on_bad_database_is_empty(fu, tmp_path):
    assert fu.pending(db_path=str(tmp_path / "nope" / "x.db")) == []
    assert fu.pending(db_path=str(tmp_path / "nope" / "x.db"), due_only=True) == []


# ── cancel ──────────────────────────────────────────────────────────────────

def test_cancel_pending_only(fu):
    rid = fu.schedule("a", 1, now=T0)["id"]
    assert fu.cancel(rid) is True
    assert _rows(fu)[0]["status"] == "cancelled"
    assert fu.cancel(rid) is False
    assert fu.cancel(12345) is False


def test_cancel_does_not_touch_finished_rows(fu):
    rid = fu.schedule("a", 1, now=T0)["id"]
    fu.mark(rid, "failed", "oops")
    assert fu.cancel(rid) is False
    assert _rows(fu)[0]["status"] == "failed"


def test_cancel_accepts_numeric_string_and_rejects_garbage(fu):
    rid = fu.schedule("a", 1, now=T0)["id"]
    assert fu.cancel("nope") is False
    assert fu.cancel(str(rid)) is True


def test_cancel_bad_database_is_false(fu, tmp_path):
    assert fu.cancel(1, db_path=str(tmp_path / "nope" / "x.db")) is False


# ── mark ────────────────────────────────────────────────────────────────────

def test_mark_sets_status_and_truncates_result(fu):
    rid = fu.schedule("a", 1, now=T0)["id"]
    assert fu.mark(rid, "done", "r" * 2500) is None
    row = _rows(fu)[0]
    assert row["status"] == "done" and row["result"] == "r" * 2000


def test_mark_unknown_status_is_ignored(fu):
    rid = fu.schedule("a", 1, now=T0)["id"]
    fu.mark(rid, "archived", "x")
    assert _rows(fu)[0]["status"] == "pending"


def test_mark_ignores_current_status(fu):
    rid = fu.schedule("a", 1, now=T0)["id"]
    fu.cancel(rid)
    fu.mark(rid, "pending")
    row = _rows(fu)[0]
    assert row["status"] == "pending" and row["result"] == ""


def test_mark_never_raises(fu, tmp_path):
    rid = fu.schedule("a", 1, now=T0)["id"]
    fu.mark(rid, "done", None)                       # unsliceable result
    assert _rows(fu)[0]["status"] == "pending"
    fu.mark(999, "done", "x")                        # unknown id
    fu.mark(1, "done", "x", db_path=str(tmp_path / "nope" / "x.db"))


# ── async_process_due ───────────────────────────────────────────────────────

async def test_due_rows_run_in_order_and_become_actions(fu, fake_hass, activity):
    fu.schedule("second thing", 3, context="ctx-b", now=T0)
    fu.schedule("first thing", 1, now=T0)
    fu.schedule("not yet", 120, now=T0)
    seen = []

    async def runner(instruction, context):
        seen.append((instruction, context))
        return f"  result of {instruction}  "

    actions = await fu.async_process_due(fake_hass, {}, runner,
                                         now=T0 + timedelta(minutes=5))
    assert seen == [("first thing", ""), ("second thing", "ctx-b")]
    assert actions == [
        {"type": "followup", "urgency": "medium",
         "message": "result of first thing", "auto_act": True},
        {"type": "followup", "urgency": "medium",
         "message": "result of second thing", "auto_act": True},
    ]
    by_text = {r["instruction"]: r for r in _rows(fu)}
    assert by_text["first thing"]["status"] == "done"
    assert by_text["first thing"]["result"] == "result of first thing"
    assert by_text["not yet"]["status"] == "pending"


async def test_activity_trail_entry(fu, fake_hass, activity):
    rid = fu.schedule("i" * 200, 1, now=T0)["id"]

    async def runner(instruction, context):
        return "z" * 300

    await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=2))
    assert activity == [{
        "category": "followup", "urgency": "low",
        "message": f"Follow-up #{rid} ran: {'i' * 120} → {'z' * 160}",
        "source": "agent"}]


async def test_action_message_is_not_truncated(fu, fake_hass, activity):
    fu.schedule("x", 1, now=T0)

    async def runner(instruction, context):
        return "y" * 3000

    actions = await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=2))
    assert actions[0]["message"] == "y" * 3000
    assert _rows(fu)[0]["result"] == "y" * 2000


@pytest.mark.parametrize("reply", ["", "   ", None])
async def test_blank_result_becomes_done(fu, fake_hass, activity, reply):
    fu.schedule("x", 1, now=T0)

    async def runner(instruction, context):
        return reply

    actions = await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=2))
    assert actions[0]["message"] == "Done."
    assert _rows(fu)[0]["result"] == "Done."


async def test_null_context_reaches_runner_as_empty(fu, fake_hass, activity):
    fu.schedule("x", 1, context=None, now=T0)
    got = []

    async def runner(instruction, context):
        got.append(context)
        return "ok"

    await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=2))
    assert got == [""]


async def test_runner_failure_marks_failed_and_continues(fu, fake_hass, activity):
    fu.schedule("breaks", 1, now=T0)
    fu.schedule("works", 2, now=T0)

    async def runner(instruction, context):
        if instruction == "breaks":
            raise RuntimeError("model offline")
        return "fine"

    actions = await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=3))
    assert [a["message"] for a in actions] == ["fine"]
    rows = {r["instruction"]: r for r in _rows(fu)}
    assert rows["breaks"]["status"] == "failed"
    assert rows["breaks"]["result"] == "model offline"
    assert rows["works"]["status"] == "done"
    assert len(activity) == 1


async def test_non_string_result_counts_as_failure(fu, fake_hass, activity):
    fu.schedule("x", 1, now=T0)

    async def runner(instruction, context):
        return 42

    assert await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=2)) == []
    assert _rows(fu)[0]["status"] == "failed"


async def test_no_runner_fails_every_due_row(fu, fake_hass):
    fu.schedule("a", 1, now=T0)
    fu.schedule("b", 1, now=T0)
    fu.schedule("later", 90, now=T0)
    actions = await fu.async_process_due(fake_hass, {}, None, now=T0 + timedelta(minutes=2))
    assert actions == []
    rows = {r["instruction"]: r for r in _rows(fu)}
    assert rows["a"]["status"] == rows["b"]["status"] == "failed"
    assert rows["a"]["result"] == "no runner available"
    assert rows["later"]["status"] == "pending"


async def test_nothing_due_does_not_call_runner(fu, fake_hass):
    fu.schedule("later", 90, now=T0)

    async def runner(instruction, context):
        raise AssertionError("must not run")

    assert await fu.async_process_due(fake_hass, {}, runner, now=T0) == []


async def test_activity_failure_is_swallowed(fu, fake_hass, load, monkeypatch):
    db = load("database")

    def boom(**kw):
        raise RuntimeError("log store down")
    monkeypatch.setattr(db, "save_activity", boom)
    fu.schedule("x", 1, now=T0)

    async def runner(instruction, context):
        return "ok"

    actions = await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=2))
    assert len(actions) == 1 and _rows(fu)[0]["status"] == "done"


async def test_loading_failure_returns_empty(fu, fake_hass, monkeypatch):
    async def broken(func, *args):
        raise RuntimeError("executor gone")
    monkeypatch.setattr(fake_hass, "async_add_executor_job", broken)

    async def runner(instruction, context):
        raise AssertionError("must not run")

    assert await fu.async_process_due(fake_hass, {}, runner) == []


async def test_database_work_goes_through_the_executor(fu, fake_hass, activity, monkeypatch):
    fu.schedule("x", 1, now=T0)
    jobs = []
    real = fake_hass.async_add_executor_job

    async def counting(func, *args):
        jobs.append(func)
        return await real(func, *args)
    monkeypatch.setattr(fake_hass, "async_add_executor_job", counting)

    async def runner(instruction, context):
        return "ok"

    await fu.async_process_due(fake_hass, {}, runner, now=T0 + timedelta(minutes=2))
    assert len(jobs) == 2                            # load due rows, mark done


async def test_explicit_db_path_is_used_throughout(fu, fake_hass, activity, tmp_path):
    other = str(tmp_path / "side.db")
    fu.schedule("side job", 1, now=T0, db_path=other)

    async def runner(instruction, context):
        return "ok"

    actions = await fu.async_process_due(fake_hass, {}, runner=runner,
                                         now=T0 + timedelta(minutes=2), db_path=other)
    assert len(actions) == 1
    assert fu.pending(db_path=other) == []
