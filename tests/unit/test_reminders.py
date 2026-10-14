"""Fresh behaviour tests for reminders.py: the SQLite reminder store, the
30-second watcher that hands due reminders to the one delivery path, and
the nova.add_reminder service handler.

Written from the behaviour spec. Every test uses its own database file.
Home Assistant is the fake core from tests/fakes.py; the timer helper
(which the harness does not stub) is replaced with a recorder, delivery is
captured on the loaded delivery module, and speech is captured on the
module's own async_announce."""
from __future__ import annotations

import ast
import asyncio
import pathlib
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeHass

SRC = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "reminders.py"
COLUMNS = {"id", "created", "label", "trigger_at", "repeat", "require_home",
           "respect_quiet", "acknowledged", "last_fired"}


class _Timer:
    def __init__(self):
        self.calls = []
        self.unsubs = 0

    def track(self, hass, action, interval):
        self.calls.append((hass, action, interval))

        def unsub():
            self.unsubs += 1
        return unsub


@pytest.fixture
def timer():
    return _Timer()


@pytest.fixture
def rem(load, monkeypatch, tmp_path, timer):
    if "jc.reminders" not in sys.modules:
        ev = types.ModuleType("homeassistant.helpers.event")
        ev.async_track_time_interval = lambda *a, **k: (lambda: None)
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    m = load("reminders")
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "store" / "reminders.db")
    monkeypatch.setattr(m, "async_track_time_interval", timer.track)
    return m


@pytest.fixture
def clock(rem, monkeypatch):
    """A settable naive-local 'now' for the store."""
    state = {"now": datetime(2026, 3, 1, 12, 0, 0)}
    monkeypatch.setattr(rem, "_now_local", lambda: state["now"])
    return state


def _rows(rem):
    conn = sqlite3.connect(str(rem.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM reminders ORDER BY id")]
    finally:
        conn.close()


def _sql(rem, sql, *args):
    conn = sqlite3.connect(str(rem.DB_PATH))
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


T9 = datetime(2026, 3, 1, 9, 0, 0)


# ── the store ─────────────────────────────────────────────────────────────────

def test_default_path_is_the_nova_reminders_db(rem, load, monkeypatch, tmp_path):
    paths = load("paths")
    asked = []

    def nova_path(name):
        asked.append(name)
        return str(tmp_path / "cfg" / "nova" / name)
    monkeypatch.setattr(paths, "nova_path", nova_path)
    monkeypatch.setattr(rem, "DB_PATH", None)
    got = rem._db_path()
    assert isinstance(got, pathlib.Path)
    assert got == tmp_path / "cfg" / "nova" / "reminders.db" and asked == ["reminders.db"]


def test_override_path_wins_and_may_be_a_string(rem, monkeypatch, tmp_path):
    assert rem._db_path() == tmp_path / "store" / "reminders.db"
    monkeypatch.setattr(rem, "DB_PATH", str(tmp_path / "s.db"))
    assert rem.add_reminder("x", T9) == 1
    assert (tmp_path / "s.db").exists()


def test_connect_creates_the_schema_with_wal(rem):
    conn = rem._connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        idx = {r[1] for r in conn.execute("PRAGMA index_list(reminders)")}
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()
    assert cols == COLUMNS
    assert mode.lower() == "wal"
    assert "idx_trigger_at" in idx


def test_add_reminder_stores_one_row(rem):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    rid = rem.add_reminder("water the plants", T9, "daily", True, False)
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    assert rid == 1
    (row,) = _rows(rem)
    assert row["id"] == 1 and row["label"] == "water the plants"
    assert row["trigger_at"] == "2026-03-01T09:00:00"
    assert row["repeat"] == "daily"
    assert (row["require_home"], row["respect_quiet"]) == (1, 0)
    assert row["acknowledged"] == 0 and row["last_fired"] is None
    created = datetime.fromisoformat(row["created"])
    assert created.tzinfo is None and before <= created <= after   # UTC, naive


def test_add_reminder_defaults_and_ids_increase(rem):
    a = rem.add_reminder("one", T9)
    b = rem.add_reminder("two", T9 + timedelta(hours=1))
    assert (a, b) == (1, 2)
    rows = _rows(rem)
    assert [(r["repeat"], r["require_home"], r["respect_quiet"]) for r in rows] == \
        [(None, 1, 1), (None, 1, 1)]


def test_flags_are_stored_by_truthiness(rem):
    rem.add_reminder("x", T9, None, "", 5)
    (row,) = _rows(rem)
    assert (row["require_home"], row["respect_quiet"]) == (0, 1)


def test_trigger_keeps_its_own_precision_and_offset(rem):
    rem.add_reminder("a", datetime(2026, 3, 1, 9, 0, 0, 250000))
    rem.add_reminder("b", datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc))
    assert [r["trigger_at"] for r in _rows(rem)] == [
        "2026-03-01T09:00:00.250000", "2026-03-01T09:00:00+00:00"]


def test_add_reminder_failure_returns_minus_one(rem, monkeypatch, tmp_path):
    assert rem.add_reminder("x", "2026-03-01T09:00:00") == -1     # not a datetime
    blocker = tmp_path / "a_file"
    blocker.write_text("")
    monkeypatch.setattr(rem, "DB_PATH", blocker / "reminders.db")
    assert rem.add_reminder("x", T9) == -1


def test_due_means_trigger_reached(rem):
    rem.add_reminder("x", T9)
    assert rem.get_due_reminders(T9 - timedelta(seconds=1)) == []
    (at,) = rem.get_due_reminders(T9)
    (later,) = rem.get_due_reminders(T9 + timedelta(days=3))
    assert at == later
    assert set(at) == COLUMNS and at["label"] == "x" and isinstance(at, dict)


def test_due_returns_every_reached_reminder(rem):
    rem.add_reminder("a", T9)
    rem.add_reminder("b", T9 + timedelta(minutes=5))
    rem.add_reminder("c", T9 + timedelta(hours=2))
    labels = {r["label"] for r in rem.get_due_reminders(T9 + timedelta(minutes=10))}
    assert labels == {"a", "b"}


def test_acknowledged_reminders_are_never_due(rem):
    rem.add_reminder("x", T9)
    _sql(rem, "UPDATE reminders SET acknowledged = 1")
    assert rem.get_due_reminders(T9 + timedelta(hours=1)) == []


def test_fired_since_the_trigger_is_not_due(rem):
    rem.add_reminder("x", T9)
    _sql(rem, "UPDATE reminders SET last_fired = ?", "2026-03-01T09:00:05")
    assert rem.get_due_reminders(T9 + timedelta(hours=1)) == []
    _sql(rem, "UPDATE reminders SET last_fired = ?", "2026-03-01T09:00:00")
    assert rem.get_due_reminders(T9 + timedelta(hours=1)) == []


def test_fired_before_the_trigger_is_due_again(rem):
    rem.add_reminder("x", T9)
    _sql(rem, "UPDATE reminders SET last_fired = ?", "2026-02-28T09:00:00")
    assert len(rem.get_due_reminders(T9)) == 1


def test_due_compares_iso_strings(rem):
    rem.add_reminder("aware", datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc))
    assert rem.get_due_reminders(datetime(2026, 3, 1, 9, 0, 0)) == []     # "...00" < "...00+00:00"
    assert len(rem.get_due_reminders(datetime(2026, 3, 1, 9, 0, 1))) == 1


def test_due_defaults_to_local_now(rem, clock):
    rem.add_reminder("x", T9)
    clock["now"] = T9 - timedelta(minutes=1)
    assert rem.get_due_reminders() == []
    clock["now"] = T9
    assert len(rem.get_due_reminders()) == 1


def test_due_read_failure_is_an_empty_list(rem, monkeypatch, tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("")
    monkeypatch.setattr(rem, "DB_PATH", blocker / "reminders.db")
    assert rem.get_due_reminders(T9) == []


def test_mark_fired_stamps_local_now_and_stops_a_one_shot(rem, clock):
    rid = rem.add_reminder("x", T9)
    clock["now"] = datetime(2026, 3, 1, 9, 0, 30)
    rem.mark_fired(rid)
    assert _rows(rem)[0]["last_fired"] == "2026-03-01T09:00:30"
    assert rem.get_due_reminders(datetime(2026, 3, 9)) == []


def test_mark_fired_unknown_id_and_failures_are_quiet(rem, clock, monkeypatch, tmp_path):
    rem.add_reminder("x", T9)
    rem.mark_fired(999)
    assert _rows(rem)[0]["last_fired"] is None
    blocker = tmp_path / "a_file"
    blocker.write_text("")
    monkeypatch.setattr(rem, "DB_PATH", blocker / "reminders.db")
    rem.mark_fired(1)                      # no exception


@pytest.mark.parametrize("repeat, step", [
    ("daily", timedelta(days=1)), ("weekly", timedelta(days=7)),
    ("hourly", timedelta(hours=1)),
])
def test_repeating_reminders_move_one_step(rem, repeat, step):
    rid = rem.add_reminder("x", T9, repeat)
    _sql(rem, "UPDATE reminders SET last_fired = ?", "2026-03-01T09:00:10")
    assert rem.get_due_reminders(T9 + timedelta(minutes=1)) == []
    (row,) = _rows(rem)
    rem._advance_repeating(row)
    (after,) = _rows(rem)
    assert after["id"] == rid
    assert after["trigger_at"] == (T9 + step).isoformat()
    assert after["last_fired"] is None


def test_advance_does_not_catch_up_to_now(rem):
    rem.add_reminder("x", T9 - timedelta(days=10), "daily")
    (row,) = _rows(rem)
    rem._advance_repeating(row)
    assert _rows(rem)[0]["trigger_at"] == (T9 - timedelta(days=9)).isoformat()
    assert len(rem.get_due_reminders(T9)) == 1           # still due: fires again


@pytest.mark.parametrize("repeat", [None, "", "weekly:MON", "monthly", "Daily"])
def test_other_repeat_values_change_nothing(rem, repeat):
    rem.add_reminder("x", T9, repeat)
    _sql(rem, "UPDATE reminders SET last_fired = ?", "2026-03-01T09:00:10")
    before = _rows(rem)
    rem._advance_repeating(dict(before[0]))
    assert _rows(rem) == before


def test_advance_failures_are_quiet(rem):
    rem._advance_repeating({"repeat": "daily", "trigger_at": "not a date", "id": 1})
    rem._advance_repeating({"repeat": "daily", "id": 1})
    rem._advance_repeating({"repeat": "daily", "trigger_at": "2026-03-01T09:00:00"})


def test_now_local_uses_the_home_assistant_timezone(rem, monkeypatch):
    utc_now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    home_tz = timezone(timedelta(hours=-5))
    monkeypatch.setattr(rem, "dt_util", types.SimpleNamespace(
        utcnow=lambda: utc_now, as_local=lambda v: v.astimezone(home_tz)))
    got = rem._now_local()
    assert got == datetime(2026, 3, 1, 7, 0) and got.tzinfo is None


# ── the watcher ───────────────────────────────────────────────────────────────

@pytest.fixture
def d(load, monkeypatch):
    mod = load("delivery")
    state = {"sent": [], "quiet": False, "quiet_calls": 0, "raise_on": None,
             "outcome": None}

    async def deliver(hass, req):
        if state["raise_on"] and state["raise_on"] in req.message:
            raise RuntimeError("delivery failed")
        state["sent"].append(req)
        return state["outcome"] or mod.Outcome(decision=mod.Decision(True, False))

    def quiet(hass):
        state["quiet_calls"] += 1
        return state["quiet"]
    monkeypatch.setattr(mod, "async_deliver", deliver)
    monkeypatch.setattr(mod, "in_quiet_hours", quiet)
    mod.state = state
    yield mod
    del mod.state


class _Getters:
    def __init__(self, honorific="", tts="tts.engine", speakers=("media_player.hall",)):
        self.values = {"honorific": honorific, "tts": tts,
                       "speakers": list(speakers) if speakers is not None else None}
        self.calls = {"honorific": 0, "tts": 0, "speakers": 0}

    def make(self, key):
        def get():
            self.calls[key] += 1
            return self.values[key]
        return get


@pytest.fixture
def home(rem, monkeypatch):
    state = {"summary": {"anyone_home": True}, "calls": 0}

    def summary(hass):
        state["calls"] += 1
        return state["summary"]
    monkeypatch.setattr(rem, "get_presence_summary", summary)
    return state


def _watcher(rem, hass, getters):
    return rem.ReminderWatcher(hass, honorific_getter=getters.make("honorific"),
                               tts_getter=getters.make("tts"),
                               speakers_getter=getters.make("speakers"))


def test_constructing_a_watcher_schedules_nothing(rem, timer):
    hass = FakeHass()
    g = _Getters()
    w = _watcher(rem, hass, g)
    assert w.hass is hass and timer.calls == [] and hass._tasks == []
    assert g.calls == {"honorific": 0, "tts": 0, "speakers": 0}


async def test_start_ticks_every_thirty_seconds_and_stop_unsubscribes(rem, timer):
    hass = FakeHass()
    w = _watcher(rem, hass, _Getters())
    await w.async_start()
    ((h, action, interval),) = timer.calls
    assert h is hass and action == w._check and interval == timedelta(seconds=30)
    assert hass._tasks == []                   # no immediate check
    await w.async_stop()
    await w.async_stop()
    assert timer.unsubs == 1


async def test_stop_before_start_is_harmless(rem, timer):
    w = _watcher(rem, FakeHass(), _Getters())
    await w.async_stop()
    assert timer.unsubs == 0


async def test_a_tick_schedules_the_check_as_a_task(rem, home, d):
    hass = FakeHass()
    w = _watcher(rem, hass, _Getters())
    assert w._check(datetime.now()) is None
    (task,) = hass._tasks
    assert asyncio.iscoroutine(task)
    await task                                   # empty store: nothing happens
    assert d.state["sent"] == []


async def test_nothing_due_reads_nothing_else(rem, home, d, clock):
    g = _Getters(honorific="sir")
    rem.add_reminder("later", clock["now"] + timedelta(hours=1))
    await _watcher(rem, FakeHass(), g)._check_async()
    assert g.calls == {"honorific": 0, "tts": 0, "speakers": 0}
    assert home["calls"] == 0 and d.state["quiet_calls"] == 0 and d.state["sent"] == []


async def test_a_due_reminder_is_delivered_with_the_reminder_request(rem, home, d, clock):
    rem.add_reminder("put the bins out", clock["now"] - timedelta(minutes=1))
    await _watcher(rem, FakeHass(), _Getters())._check_async()
    (req,) = d.state["sent"]
    assert isinstance(req, d.DeliveryRequest)
    assert req.message == "Reminder: put the bins out."
    assert req.source == "reminder" and req.category == "reminder"
    assert req.requested_by_user is True and req.quiet_ok is False
    assert req.speakers == ("media_player.hall",) and req.tts_entity == "tts.engine"
    assert req.tts_context == "reminder"
    assert req.urgency == "medium" and req.push == d.PUSH_FALLBACK
    assert req.phone_only is False and req.title == "" and req.spoken == ""


async def test_the_honorific_leads_verbatim(rem, home, d, clock):
    rem.add_reminder("call the plumber", clock["now"])
    await _watcher(rem, FakeHass(), _Getters(honorific="sir"))._check_async()
    assert d.state["sent"][0].message == "sir, reminder: call the plumber."


async def test_missing_speakers_and_engine_become_empty(rem, home, d, clock):
    rem.add_reminder("x", clock["now"])
    await _watcher(rem, FakeHass(), _Getters(tts=None, speakers=None))._check_async()
    (req,) = d.state["sent"]
    assert req.speakers == () and req.tts_entity == ""


async def test_quiet_hours_hold_a_reminder_until_they_end(rem, home, d, clock):
    rem.add_reminder("x", clock["now"])
    d.state["quiet"] = True
    w = _watcher(rem, FakeHass(), _Getters())
    await w._check_async()
    assert d.state["sent"] == [] and len(rem.get_due_reminders()) == 1
    d.state["quiet"] = False
    await w._check_async()
    assert len(d.state["sent"]) == 1 and rem.get_due_reminders() == []


async def test_a_reminder_set_for_quiet_hours_goes_out(rem, home, d, clock):
    rem.add_reminder("x", clock["now"], None, True, False)
    d.state["quiet"] = True
    await _watcher(rem, FakeHass(), _Getters())._check_async()
    (req,) = d.state["sent"]
    assert req.quiet_ok is True
    assert d.state["quiet_calls"] == 0        # quiet hours not even consulted


async def test_quiet_hours_are_checked_per_respecting_reminder(rem, home, d, clock):
    for label in ("a", "b", "c"):
        rem.add_reminder(label, clock["now"])
    await _watcher(rem, FakeHass(), _Getters())._check_async()
    assert d.state["quiet_calls"] == 3 and len(d.state["sent"]) == 3


async def test_nobody_home_holds_a_home_only_reminder(rem, home, d, clock):
    rem.add_reminder("home only", clock["now"])
    rem.add_reminder("anywhere", clock["now"], None, False)
    home["summary"] = {"anyone_home": False}
    await _watcher(rem, FakeHass(), _Getters())._check_async()
    assert [r.message for r in d.state["sent"]] == ["Reminder: anywhere."]
    assert [r["label"] for r in rem.get_due_reminders()] == ["home only"]


async def test_a_summary_without_the_flag_counts_as_nobody_home(rem, home, d, clock):
    rem.add_reminder("x", clock["now"])
    home["summary"] = {}
    await _watcher(rem, FakeHass(), _Getters())._check_async()
    assert d.state["sent"] == []


async def test_getters_and_presence_are_read_once_per_tick(rem, home, d, clock):
    for label in ("a", "b"):
        rem.add_reminder(label, clock["now"])
    g = _Getters(honorific="sir")
    await _watcher(rem, FakeHass(), g)._check_async()
    assert g.calls == {"honorific": 1, "tts": 1, "speakers": 1}
    assert home["calls"] == 1 and len(d.state["sent"]) == 2


async def test_store_work_runs_in_the_executor_in_order(rem, home, d, monkeypatch):
    due = [{"id": 7, "label": "x", "require_home": 1, "respect_quiet": 1,
            "repeat": "daily", "trigger_at": "2026-03-01T09:00:00"}]
    order = []
    monkeypatch.setattr(rem, "get_due_reminders", lambda: [dict(r) for r in due])
    monkeypatch.setattr(rem, "mark_fired", lambda rid: order.append(("mark", rid)))
    monkeypatch.setattr(rem, "_advance_repeating",
                        lambda r: order.append(("advance", r["id"], r["label"])))
    hass = FakeHass()
    jobs = []

    async def executor(fn, *args):
        jobs.append((getattr(fn, "__name__", fn), args))
        return fn(*args)
    hass.async_add_executor_job = executor
    real_deliver = d.async_deliver

    async def deliver(h, req):
        order.append(("deliver", req.message))
        return await real_deliver(h, req)
    monkeypatch.setattr(d, "async_deliver", deliver)
    await _watcher(rem, hass, _Getters())._check_async()
    assert order == [("deliver", "Reminder: x."), ("mark", 7), ("advance", 7, "x")]
    assert [args for _n, args in jobs] == [(), (7,), (due[0],)]


async def test_marked_fired_whatever_delivery_decided(rem, home, d, clock):
    rem.add_reminder("x", clock["now"])
    d.state["outcome"] = d.Outcome(decision=d.Decision(False, False, "held"))
    await _watcher(rem, FakeHass(), _Getters())._check_async()
    assert rem.get_due_reminders() == []
    assert _rows(rem)[0]["last_fired"] == clock["now"].isoformat()


async def test_a_one_shot_fires_once(rem, home, d, clock):
    rem.add_reminder("x", clock["now"])
    w = _watcher(rem, FakeHass(), _Getters())
    await w._check_async()
    clock["now"] += timedelta(seconds=30)
    await w._check_async()
    assert len(d.state["sent"]) == 1


async def test_a_daily_reminder_moves_to_tomorrow(rem, home, d, clock):
    first = clock["now"] - timedelta(minutes=2)
    rem.add_reminder("x", first, "daily")
    w = _watcher(rem, FakeHass(), _Getters())
    await w._check_async()
    (row,) = _rows(rem)
    assert row["trigger_at"] == (first + timedelta(days=1)).isoformat()
    assert row["last_fired"] is None
    await w._check_async()
    assert len(d.state["sent"]) == 1
    clock["now"] = first + timedelta(days=1)
    await w._check_async()
    assert len(d.state["sent"]) == 2


async def test_a_delivery_failure_ends_the_tick(rem, home, d, clock):
    rem.add_reminder("first", clock["now"])
    rem.add_reminder("second", clock["now"])
    d.state["raise_on"] = "first"
    with pytest.raises(RuntimeError):
        await _watcher(rem, FakeHass(), _Getters())._check_async()
    assert d.state["sent"] == []
    assert {r["label"] for r in rem.get_due_reminders()} == {"first", "second"}


# ── the add_reminder service ──────────────────────────────────────────────────

@pytest.fixture
def spoken(rem, monkeypatch):
    said = []

    async def announce(hass, text, tts_entity, speakers, *args, **kwargs):
        said.append({"text": text, "tts": tts_entity, "speakers": speakers,
                     "args": args, "kwargs": kwargs})
        return True
    monkeypatch.setattr(rem, "async_announce", announce)
    return said


def _call(**data):
    return types.SimpleNamespace(data=data)


async def test_service_stores_and_confirms(rem, spoken):
    hass = FakeHass()
    out = await rem.async_add_reminder_service(
        hass, _call(label="feed the cat", trigger_at="2026-03-02T08:30:00"),
        "sir", "tts.engine", ["media_player.hall"])
    assert out == {"success": True, "reminder_id": 1}
    (row,) = _rows(rem)
    assert row["label"] == "feed the cat" and row["trigger_at"] == "2026-03-02T08:30:00"
    assert (row["repeat"], row["require_home"], row["respect_quiet"]) == (None, 1, 1)
    (s,) = spoken
    assert s["text"] == "Reminder saved, sir. I'll let you know."
    assert s["tts"] == "tts.engine" and s["speakers"] == ["media_player.hall"]
    assert s["kwargs"] == {"context": "reminder"} and s["args"] == ()


async def test_service_without_honorific(rem, spoken):
    await rem.async_add_reminder_service(
        FakeHass(), _call(label="x", trigger_at="2026-03-02T08:30:00"), "", None, [])
    assert spoken[0]["text"] == "Reminder saved. I'll let you know."
    assert spoken[0]["tts"] is None


async def test_service_passes_options_through(rem, spoken):
    out = await rem.async_add_reminder_service(
        FakeHass(), _call(label="x", trigger_at="2026-03-02T08:30:00", repeat="weekly",
                          require_home=False, respect_quiet=False), "sir", None, [])
    assert out["success"] is True
    (row,) = _rows(rem)
    assert (row["repeat"], row["require_home"], row["respect_quiet"]) == ("weekly", 0, 0)


async def test_service_calls_the_store_in_the_executor(rem, spoken, monkeypatch):
    seen = []
    monkeypatch.setattr(rem, "add_reminder", lambda *a, **k: seen.append((a, k)) or 42)
    hass = FakeHass()
    jobs = []
    real = hass.async_add_executor_job

    async def executor(fn, *args):
        jobs.append(fn)
        return await real(fn, *args)
    hass.async_add_executor_job = executor
    out = await rem.async_add_reminder_service(
        hass, _call(label="x", trigger_at="2026-03-02T08:30:00", repeat="hourly"),
        "sir", None, [])
    assert out == {"success": True, "reminder_id": 42}
    assert len(jobs) == 1
    ((args, kwargs),) = seen
    assert kwargs == {}
    assert args == ("x", datetime(2026, 3, 2, 8, 30), "hourly", True, True)


@pytest.mark.parametrize("when", ["08:00", "tomorrow", "", "2026-13-01T00:00:00"])
async def test_service_rejects_an_unreadable_time(rem, spoken, when):
    out = await rem.async_add_reminder_service(
        FakeHass(), _call(label="x", trigger_at=when), "sir", None, [])
    assert out == {"success": False, "error": "invalid_datetime"}
    assert spoken == []
    assert not rem.DB_PATH.exists() or _rows(rem) == []


async def test_service_reports_a_store_failure_without_speaking(rem, spoken, monkeypatch):
    monkeypatch.setattr(rem, "add_reminder", lambda *a: -1)
    out = await rem.async_add_reminder_service(
        FakeHass(), _call(label="x", trigger_at="2026-03-02T08:30:00"), "sir", None, [])
    assert out == {"success": False, "error": "db_error"}
    assert spoken == []


async def test_service_accepts_a_time_in_the_past(rem, spoken, clock):
    out = await rem.async_add_reminder_service(
        FakeHass(), _call(label="x", trigger_at="2020-01-01T00:00:00"), "", None, [])
    assert out["success"] is True
    assert len(rem.get_due_reminders()) == 1


# ── source-level contracts the rest of the suite relies on ───────────────────

def _calls(tree, name):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)) == name:
                out.append(ast.unparse(f))
    return out


def test_source_contracts():
    text = SRC.read_text(encoding="utf-8")
    tree = ast.parse(text)
    assert _calls(tree, "async_deliver") == ["delivery.async_deliver"]
    assert _calls(tree, "async_announce") == ["async_announce"]
    assert _calls(tree, "async_call") == []
    assert "paths.nova_path" in _calls(tree, "nova_path")
    assert 'nova_path("reminders.db")' in text
    for banned in ("CREATE TABLE", "QUIET_START", "def _in_quiet_hours",
                   "sqlite3.connect", "hass.data", "/config"):
        assert banned not in text
