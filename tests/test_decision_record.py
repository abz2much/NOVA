"""Decision records, from the store out to the code that feeds and judges it.

Each proactive decision leaves one row. Its outcome is written once and then
frozen. The callers (cognition, the suggestion store and the intrusion
call-off) must pass their facts through untouched, and a broken store must
never break the decision it was meant to describe.
"""
import sqlite3
from contextlib import closing

import pytest

NOW = 1_700_000_000.0

# Full suggestions table as the analyser writes and later updates it.
SUGGESTION_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, description TEXT, "
    "automation_yaml TEXT, confidence REAL, pattern_count INTEGER, pattern_type TEXT, "
    "entity_ids TEXT, details TEXT, status TEXT, dismissed_at TEXT, approved_at TEXT"
)


class Spy:
    """Stands in for a function: keeps every call and answers ``result``."""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _sql(path, statement, params=()):
    """Run one statement on a database file, commit and close it."""
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(statement, params).fetchall()
        conn.commit()
    return rows


def _explode(*_args, **_kwargs):
    raise RuntimeError("store unavailable")


@pytest.fixture
def store(load, tmp_path, monkeypatch):
    """The decision record module, writing to a private database."""
    module = load("decision_record")
    monkeypatch.setattr(module, "_DEFAULT_DB", str(tmp_path / "records.db"))
    return module


@pytest.fixture
def analyser(load, tmp_path, monkeypatch):
    """A pattern analyser on an empty suggestions table, with automation
    generation stubbed out."""
    patterns = load("pattern_analyzer")
    path = str(tmp_path / "suggestions.db")
    _sql(path, f"CREATE TABLE suggestions ({SUGGESTION_COLUMNS})")
    engine = patterns.PatternAnalyzer()
    engine._db = path
    monkeypatch.setattr(engine, "_generate_automation", lambda _pattern: "{}")
    engine.module = patterns
    return engine


def _seed_suggestion(engine, suggestion_id):
    _sql(engine._db, "INSERT INTO suggestions (id, status) VALUES (?, 'pending')",
         (suggestion_id,))


def _status_of(engine, suggestion_id):
    found = _sql(engine._db, "SELECT status FROM suggestions WHERE id = ?", (suggestion_id,))
    return found[0][0] if found else None


def _porch_pattern(engine, **overrides):
    fields = dict(pattern_type="time_routine", description="Porch light at 18:00",
                  entity_ids=["light.porch"], confidence=0.82, occurrences=6)
    fields.update(overrides)
    return engine.module.DetectedPattern(**fields)


def _ids(rows):
    return [row["id"] for row in rows]


# ── The store ───────────────────────────────────────────────────────────────

def test_each_part_reads_back_separately(store):
    seen = {"event": "Gym", "mins_to": 20, "presence": "home"}
    meant = {"predicted": "leaving soon", "pattern": "gym_mon"}
    new_id = store.record("anticipation_departure", observation=seen,
                          interpretation=meant, evidence={"travel_min": 12},
                          decision="announce departure heads-up",
                          reason="recurring event + travel time", confidence=0.72)
    assert type(new_id) is int and new_id >= 1

    row = store.get(new_id)
    # The facts and what Nova made of them stay two separate objects.
    assert row["observation"] == seen
    assert row["interpretation"] == meant
    assert row["evidence"] == {"travel_min": 12}
    assert row["decision"] == "announce departure heads-up"
    assert row["reason"] == "recurring event + travel time"
    assert row["confidence"] == pytest.approx(0.72, abs=1e-9)
    # A rule-based decision carries no model metadata at all.
    assert (row["model"], row["tokens"], row["latency_ms"]) == (None, None, None)
    # Nothing has judged it yet.
    assert (row["outcome"], row["outcome_source"], row["outcome_ts"]) == (None, None, None)


def test_an_explicit_path_wins_over_the_default(store, tmp_path):
    elsewhere = str(tmp_path / "elsewhere.db")
    new_id = store.record("intrusion", decision="alert", db_path=elsewhere)
    assert store.get(new_id, db_path=elsewhere)["decision"] == "alert"
    assert store.stats()["total"] == 0


@pytest.mark.parametrize("first, source, second", [
    ("wrong", "dismiss_intrusion", "good"),
    ("good", "installed", "wrong"),
    ("unnecessary", "phone", "good"),
])
def test_the_first_verdict_is_final(store, first, source, second):
    new_id = store.record("intrusion", decision="alert", reason="door open while away")
    assert store.set_outcome(new_id, first, source=source, ts=NOW) is True
    judged = store.get(new_id)
    assert (judged["outcome"], judged["outcome_source"]) == (first, source)
    assert judged["outcome_ts"] == NOW

    # Trying again is refused and leaves every outcome column alone.
    assert store.set_outcome(new_id, second, source="later", ts=NOW + 5) is False
    again = store.get(new_id)
    assert (again["outcome"], again["outcome_source"], again["outcome_ts"]) == (first, source, NOW)


def test_verdict_constants_hold_their_stored_words(store):
    assert (store.OUTCOME_GOOD, store.OUTCOME_UNNECESSARY, store.OUTCOME_WRONG) == (
        "good", "unnecessary", "wrong")


@pytest.mark.parametrize("missing", [99999, None])
def test_judging_an_unknown_record_changes_nothing(store, missing):
    kept = store.record("suggestion")
    assert store.set_outcome(missing, store.OUTCOME_UNNECESSARY) is False
    assert store.set_outcome(missing, store.OUTCOME_WRONG) is False
    assert store.set_outcome_checked(missing, store.OUTCOME_GOOD) == "not_found"
    assert store.get(kept)["outcome"] is None


def test_recent_lists_newest_first_and_filters(store):
    early = store.record("anticipation_departure", ts=100.0)
    middle = store.record("intrusion", ts=200.0)
    late = store.record("anticipation_departure", ts=300.0)
    store.set_outcome(early, store.OUTCOME_GOOD)

    assert _ids(store.recent()) == [late, middle, early]
    assert store.recent()[0]["kind"] == "anticipation_departure"
    by_kind = store.recent(kind="intrusion")
    assert _ids(by_kind) == [middle]
    assert {row["kind"] for row in by_kind} == {"intrusion"}
    # The judged one drops out of the open list.
    assert _ids(store.recent(only_unjudged=True)) == [late, middle]
    assert _ids(store.recent(limit=1)) == [late]


def test_stats_tally_every_verdict(store):
    liked = store.record("suggestion")
    ignored = store.record("suggestion")
    store.record("intrusion")   # still open
    store.set_outcome(liked, store.OUTCOME_GOOD)
    store.set_outcome(ignored, store.OUTCOME_UNNECESSARY)
    assert store.stats() == {"total": 3, "judged": 2, "good": 1,
                             "unnecessary": 1, "wrong": 0}


# Each entry point and the answer it must give when the database cannot open.
_SOFT_FAILURES = [
    ("record", lambda dr, db: dr.record("intrusion", db_path=db), None),
    ("get", lambda dr, db: dr.get(1, db_path=db), None),
    ("recent", lambda dr, db: dr.recent(db_path=db), []),
    ("page", lambda dr, db: dr.page(db_path=db), {"items": [], "next_cursor": None}),
    ("set_outcome", lambda dr, db: dr.set_outcome(1, "good", db_path=db), False),
    ("checked", lambda dr, db: dr.set_outcome_checked(1, "good", db_path=db), "not_found"),
    ("by_ref", lambda dr, db: dr.set_outcome_by_ref("suggestion:1", "good", db_path=db), False),
    ("recent_kind", lambda dr, db: dr.set_outcome_recent("intrusion", "wrong", db_path=db), False),
]


@pytest.mark.parametrize("call, expected", [(c, e) for _, c, e in _SOFT_FAILURES],
                         ids=[name for name, _, _ in _SOFT_FAILURES])
def test_an_unopenable_database_gives_the_quiet_answer(store, tmp_path, call, expected):
    # A plain file sits where the database folder should be.
    blocker = tmp_path / "plain_file"
    blocker.write_text("not a folder")
    assert call(store, str(blocker / "records.db")) == expected


# ── Cognition hands its decisions over ─────────────────────────────────────

def test_cognition_passes_every_part_through(load, store, monkeypatch):
    cognition = load("cognition")
    spy = Spy(result=41)
    monkeypatch.setattr(store, "record", spy)

    returned = cognition._log_decision(
        "anticipation_departure", {"event": "Gym", "minutes_until": 20},
        {"predicted": "leave soon"}, "announce departure heads-up",
        "recurring calendar event", confidence=0.7)

    assert returned == 41
    (args, kwargs), = spy.calls
    assert args == ("anticipation_departure",)
    assert kwargs["observation"] == {"event": "Gym", "minutes_until": 20}
    assert kwargs["interpretation"] == {"predicted": "leave soon"}
    assert kwargs["decision"] == "announce departure heads-up"
    assert kwargs["reason"] == "recurring calendar event"
    assert kwargs["confidence"] == pytest.approx(0.7, abs=1e-9)
    assert kwargs["ref"] is None


def test_cognition_shrugs_off_a_failing_store(load, store, monkeypatch):
    cognition = load("cognition")
    monkeypatch.setattr(store, "record", _explode)
    # Recording can never break the decision itself.
    assert cognition._log_decision("anticipation_overdue", {}, {}, "x", "y") is None


# ── Suggestions ─────────────────────────────────────────────────────────────

def test_a_new_suggestion_is_recorded_as_a_proposal(analyser, store, monkeypatch):
    spy = Spy(result=1)
    monkeypatch.setattr(store, "record", spy)

    assert analyser._store_suggestion(_porch_pattern(analyser)) is True
    (args, kwargs), = spy.calls
    assert args == ("suggestion",)
    assert kwargs["observation"] == {"pattern_type": "time_routine",
                                     "entities": ["light.porch"], "occurrences": 6}
    assert kwargs["interpretation"] == {"suggested": "Porch light at 18:00"}
    assert kwargs["decision"] == "propose automation"
    assert kwargs["reason"] == "recurring observed behavior"
    assert kwargs["confidence"] == pytest.approx(0.82, abs=1e-9)
    assert kwargs["ref"] == "suggestion:1"


def test_a_failing_store_still_saves_the_suggestion(analyser, store, monkeypatch):
    monkeypatch.setattr(store, "record", _explode)
    pattern = _porch_pattern(analyser, description="X", entity_ids=[],
                             confidence=0.5, occurrences=3)
    # The record has its own guard, so the suggestion result stays True.
    assert analyser._store_suggestion(pattern) is True
    assert _status_of(analyser, 1) == "pending"


def test_proposal_then_dismissal_against_the_real_store(analyser, store):
    assert analyser._store_suggestion(_porch_pattern(analyser)) is True
    # The same behaviour again is a refresh, not a second decision.
    assert analyser._store_suggestion(_porch_pattern(analyser)) is False
    (proposal,) = store.recent(kind="suggestion")
    assert proposal["ref"] == "suggestion:1"

    assert analyser.dismiss_suggestion(1) is True
    judged = store.get(proposal["id"])
    assert (judged["outcome"], judged["outcome_source"]) == ("unnecessary", "dismiss_suggestion")
    assert _status_of(analyser, 1) == "dismissed"


@pytest.mark.parametrize("act, sid, verdict, source, status", [
    (lambda engine, sid: engine.dismiss_suggestion(sid), 7, "unnecessary",
     "dismiss_suggestion", "dismissed"),
    (lambda engine, sid: engine.mark_installed(sid, "automation.foo"), 9, "good",
     "installed", "installed"),
], ids=["dismissed", "installed"])
def test_a_persons_choice_judges_the_suggestion(analyser, store, monkeypatch,
                                                act, sid, verdict, source, status):
    spy = Spy()
    monkeypatch.setattr(store, "set_outcome_by_ref", spy)
    _seed_suggestion(analyser, sid)
    act(analyser, sid)
    assert (("suggestion:%d" % sid, verdict, source), {}) in spy.calls
    assert _status_of(analyser, sid) == status


def test_installing_also_opens_an_automation_trial(load, analyser, monkeypatch, tmp_path):
    """The trial is a separate record from the acceptance verdict: it tracks
    whether the automation then runs, not whether it was accepted."""
    trials = load("automation_trials")
    trials_db = str(tmp_path / "trials.db")
    monkeypatch.setattr(trials, "_DEFAULT_DB", trials_db)
    _seed_suggestion(analyser, 12)

    analyser.mark_installed(12, "nova_auto_porch_light")

    (trial,) = trials.list_trials(db_path=trials_db)
    assert (trial["suggestion_id"], trial["automation_id"]) == (12, "nova_auto_porch_light")
    # Being installed is acceptance, not evidence that it performs.
    assert trial["run_count"] == 0
    assert trial["manual_outcome"] is None


def test_a_failing_trial_does_not_undo_the_install(load, analyser, store, monkeypatch):
    trials = load("automation_trials")
    monkeypatch.setattr(trials, "create", _explode)
    spy = Spy()
    monkeypatch.setattr(store, "set_outcome_by_ref", spy)
    _seed_suggestion(analyser, 13)

    analyser.mark_installed(13, "nova_auto_x")

    assert _status_of(analyser, 13) == "installed"
    assert spy.calls == [(("suggestion:13", "good", "installed"), {})]


# ── Linking outcomes back ──────────────────────────────────────────────────

def test_by_ref_judges_once_and_only_known_refs(store):
    new_id = store.record("suggestion", ref="suggestion:11")
    assert store.set_outcome_by_ref("suggestion:11", "unnecessary", "dismiss_suggestion") is True
    row = store.get(new_id)
    assert (row["outcome"], row["outcome_source"]) == ("unnecessary", "dismiss_suggestion")

    assert store.set_outcome_by_ref("suggestion:999", "wrong") is False   # never recorded
    assert store.set_outcome_by_ref("suggestion:11", "good") is False     # already judged
    for blank in ("", None):
        assert store.set_outcome_by_ref(blank, "good") is False
    assert store.get(new_id)["outcome"] == "unnecessary"


def test_recent_kind_only_reaches_back_max_age(store, monkeypatch):
    monkeypatch.setattr(store.time, "time", lambda: NOW)
    stale = store.record("intrusion", ts=NOW - 10_000)
    edge = store.record("intrusion", ts=NOW - 3_600)
    fresh = store.record("intrusion", ts=NOW - 60)
    other = store.record("hazard", ts=NOW - 30)

    def judge():
        return store.set_outcome_recent("intrusion", "wrong", "dismiss_intrusion", max_age=3600)

    assert judge() is True
    assert store.get(fresh)["outcome"] == "wrong"
    # A record exactly max_age old is still in reach.
    assert judge() is True
    assert store.get(edge)["outcome"] == "wrong"
    # Nothing open is left inside the window.
    assert judge() is False
    assert store.get(stale)["outcome"] is None
    assert store.get(other)["outcome"] is None


def test_calling_off_an_intrusion_marks_it_wrong(load, store, monkeypatch):
    intrusion = load("intrusion")
    # Every global the call-off touches goes back afterwards.
    monkeypatch.setattr(intrusion, "_called_off_until", 0.0)
    monkeypatch.setattr(intrusion, "_acknowledged_ts", 0.0)
    monkeypatch.setattr(intrusion, "_last_decision_id", None)
    monkeypatch.setattr(intrusion, "_false_alarms", [])
    spy = Spy()
    monkeypatch.setattr(store, "set_outcome_recent", spy)

    result = intrusion.dismiss_intrusion("that's me")

    assert spy.calls == [(("intrusion", "wrong", "dismiss_intrusion"), {"max_age": 7200.0})]
    assert result["ok"] is True
    assert result["recorded"]["reason"] == "that's me"
    assert intrusion.is_called_off() is True


# ── The decision browser ───────────────────────────────────────────────────

def test_checked_setter_names_each_result(store):
    new_id = store.record("intrusion")
    assert [
        store.set_outcome_checked(new_id, "wrong", "panel"),
        store.set_outcome_checked(new_id, "good", "panel"),
        store.set_outcome_checked(99999, "good", "panel"),
    ] == ["ok", "already_judged", "not_found"]
    # Only the first one landed.
    assert store.get(new_id)["outcome"] == "wrong"


def _walk(store, size, **filters):
    """Every page in turn, following the cursor until it runs out."""
    pages, cursor = [], {}
    while True:
        chunk = store.page(limit=size, **filters, **cursor)
        pages.append(chunk)
        if chunk["next_cursor"] is None:
            return pages
        cursor = {"cursor_ts": chunk["next_cursor"]["ts"],
                  "cursor_id": chunk["next_cursor"]["id"]}


def test_pages_run_newest_first_until_the_end(store):
    made = [store.record("intrusion", ts=100.0 + n) for n in range(5)]
    pages = _walk(store, 2)

    assert [_ids(p["items"]) for p in pages] == [[made[4], made[3]], [made[2], made[1]], [made[0]]]
    assert [p["next_cursor"] for p in pages] == [
        {"ts": 103.0, "id": made[3]}, {"ts": 101.0, "id": made[1]}, None]


def test_equal_times_are_split_by_id(store):
    """Rows sharing a timestamp are neither dropped nor repeated across pages."""
    first, second, third = (store.record("intrusion", ts=100.0) for _ in range(3))
    assert first < second < third
    pages = _walk(store, 2)
    assert [_ids(p["items"]) for p in pages] == [[third, second], [first]]
    assert pages[-1]["next_cursor"] is None


def test_page_filters_by_kind_and_open(store):
    liked = store.record("suggestion", ts=100.0)
    alarm = store.record("intrusion", ts=200.0)
    store.set_outcome(liked, store.OUTCOME_GOOD)

    assert _ids(store.page(kind="suggestion")["items"]) == [liked]
    assert _ids(store.page(only_unjudged=True)["items"]) == [alarm]


def test_page_rows_are_summaries_without_the_parts(store):
    """The list view leaves out the JSON parts; get() fetches those."""
    store.record("intrusion", observation={"secret": "x"})
    (row,) = store.page()["items"]
    assert not {"observation", "interpretation", "evidence"} & set(row)
    assert sorted(row) == sorted(["id", "ts", "kind", "decision", "reason", "confidence",
                                  "model", "tokens", "latency_ms", "outcome"])


def test_an_empty_store_has_one_empty_page(store):
    assert store.page() == {"items": [], "next_cursor": None}
