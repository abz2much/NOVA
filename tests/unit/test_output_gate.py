"""Behaviour of output_gate.py: the last check before Nova speaks unprompted.

Pins the order of the checks (critical first, then mutes, habituation, the
hourly cap and repeats), what an announcement record leaves behind, the
shush and unshush results, the saved mutes file and the status summary."""
import asyncio
import json
import pathlib
import types

import pytest


@pytest.fixture
def og(load, monkeypatch, tmp_path):
    mod = load("output_gate")
    path = tmp_path / "nova" / "output_mutes.json"
    monkeypatch.setattr(mod, "_mutes_path", lambda: str(path))
    state = mod._STATE
    for box in (state.history, state.recent_messages, state.muted_entities,
                state.muted_categories):
        box.clear()
    state.mute_all = False
    mod._BUDGET_CACHE.update(ts=0.0, mult=1.0)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(mod, "_now", lambda: clock["now"])
    mod.clock = clock
    hab = load("habituation")
    calls = types.SimpleNamespace(recorded=[], quiet=set(), exempt_kinds={"safety"})
    monkeypatch.setattr(hab, "exempt", lambda *, urgency="", kind="", entity_id="":
                        urgency == "critical" or kind in calls.exempt_kinds)
    monkeypatch.setattr(hab, "is_quiet", lambda key: key in calls.quiet)
    monkeypatch.setattr(hab, "with_note", lambda key, message: message + " [noted:" + key + "]")
    monkeypatch.setattr(hab, "record", lambda key, entity_id="": calls.recorded.append(
        (key, entity_id)))
    mod.hab = calls
    db = load("database")
    activity = []
    monkeypatch.setattr(db, "save_activity", lambda **kw: activity.append(kw))
    mod.activity = activity
    cfg = load("nova_config")
    settings = {}
    monkeypatch.setattr(cfg, "get", lambda k, d=None: settings.get(k, d))
    mod.settings = settings
    yield mod
    for box in (state.history, state.recent_messages, state.muted_entities,
                state.muted_categories):
        box.clear()
    state.mute_all = False
    mod._BUDGET_CACHE.update(ts=0.0, mult=1.0)


def _ask(og, message="The garage door is open", entity_id="cover.garage",
         category="security", urgency="medium", **kw):
    return og.can_announce(entity_id=entity_id, category=category, urgency=urgency,
                           message=message, **kw)


def _said(og, message, entity_id="sensor.x", category="info", urgency="low",
          spoken=True, **kw):
    og.record_announcement(entity_id=entity_id, category=category, urgency=urgency,
                           message=message, was_spoken=spoken, **kw)


# ── constants and state ──────────────────────────────────────────────────────

def test_defaults(og):
    assert og.DEFAULT_MAX_PER_HOUR == 6
    assert og.DEFAULT_DEDUP_MINUTES == 10
    assert og.MUTES_FILE == "output_mutes.json"
    fresh = og.GateState()
    assert fresh.history.maxlen == 100 and fresh.recent_messages.maxlen == 20
    assert fresh.mute_all is False and fresh.muted_entities == set()
    ann = og.Announcement(timestamp=1.0, entity_id="e", category="c", urgency="u",
                          message="m")
    assert ann.was_spoken is True


# ── the gate ─────────────────────────────────────────────────────────────────

def test_open_gate(og):
    assert _ask(og) == (True, "ok")


def test_critical_passes_every_check(og):
    og._STATE.mute_all = True
    og._STATE.muted_entities.add("cover.garage")
    og._STATE.muted_categories.add("security")
    og.hab.quiet.add("cover.garage")
    for i in range(10):
        _said(og, f"Message number {i} here")
    assert _ask(og, urgency="critical") == (True, "critical bypass")


def test_check_order_and_reasons(og):
    og._STATE.mute_all = True
    assert _ask(og) == (False, "blanket shush active")
    og._STATE.mute_all = False
    og._STATE.muted_entities.add("cover.garage")
    og._STATE.muted_categories.add("security")
    assert _ask(og) == (False, "entity cover.garage is muted")
    og._STATE.muted_entities.clear()
    assert _ask(og) == (False, "category security is muted")
    og._STATE.muted_categories.clear()
    og.hab.quiet.add("cover.garage")
    assert _ask(og) == (False, "cover.garage is normal for this home (came up days running)")


def test_habituation_key_and_exemptions(og):
    og.hab.quiet.add("garage-door")
    assert _ask(og, habit_key="garage-door")[0] is False
    assert _ask(og, habit_key="")[0] is True                 # no key, the source counts
    og.hab.quiet.add("cover.garage")
    assert _ask(og, category="safety")[0] is True             # exempt kind
    assert _ask(og, habit_key="other")[0] is True


def test_habituation_failure_lets_it_through(og, load, monkeypatch):
    def boom(key):
        raise RuntimeError
    monkeypatch.setattr(load("habituation"), "is_quiet", boom)
    assert _ask(og) == (True, "ok")


def test_hourly_cap(og):
    for i in range(6):
        _said(og, f"Distinct words {i} alpha{i} bravo{i}")
    assert _ask(og) == (False, "rate limit (6/6/hour)")
    assert _ask(og, urgency="high") == (True, "ok")
    assert _ask(og, max_per_hour=7) == (True, "ok")
    og.clock["now"] += 3601
    assert _ask(og) == (True, "ok")


def test_cap_counts_only_spoken(og):
    for i in range(10):
        _said(og, f"Quiet number {i}", spoken=False)
    assert _ask(og) == (True, "ok")


def test_adaptive_budget(og, load, monkeypatch):
    dr = load("decision_record")
    asked = []

    def budget():
        asked.append(1)
        return {"multiplier": 0.5}
    monkeypatch.setattr(dr, "interruption_budget", budget)
    for i in range(3):
        _said(og, f"Distinct words {i} alpha{i} bravo{i}")
    assert _ask(og) == (True, "ok")                           # off by default
    og.settings["adaptive_interruption_budget"] = True
    assert _ask(og) == (False, "rate limit (3/3/hour)")
    assert _ask(og)[0] is False and len(asked) == 1            # cached
    og.clock["now"] += 61
    monkeypatch.setattr(dr, "interruption_budget", lambda: {"multiplier": 0.01})
    assert _ask(og, max_per_hour=6) == (False, "rate limit (3/1/hour)")


def test_budget_multiplier_failures(og, load, monkeypatch):
    og.settings["adaptive_interruption_budget"] = True

    def boom():
        raise RuntimeError
    monkeypatch.setattr(load("decision_record"), "interruption_budget", boom)
    assert og._budget_multiplier() == 1.0
    assert og._BUDGET_CACHE == {"ts": og.clock["now"], "mult": 1.0}

    def cfg_boom(*a, **k):
        raise RuntimeError
    monkeypatch.setattr(load("nova_config"), "get", cfg_boom)
    assert og._budget_multiplier() == 1.0


def test_repeats(og):
    _said(og, "The garage door has been left open")
    assert _ask(og, message="Garage door left OPEN!") == (False, "duplicate of recent message")
    assert _ask(og, message="Kitchen lights are on") == (True, "ok")
    _said(og, "Something else entirely", dedup_key="garage")
    assert _ask(og, message="Unrelated words here", dedup_key="garage") == \
        (False, "repeat of a recent message")
    assert _ask(og, message="Unrelated words here", dedup_key="other") == (True, "ok")
    og.clock["now"] += 601
    assert _ask(og, message="Garage door left OPEN!") == (True, "ok")
    assert _ask(og, message="Garage door left OPEN!", dedup_minutes=11) == \
        (False, "duplicate of recent message")


def test_suppressed_messages_do_not_count_as_repeats(og):
    _said(og, "The garage door has been left open", spoken=False, dedup_key="g")
    assert _ask(og, message="The garage door has been left open", dedup_key="g") == (True, "ok")


@pytest.mark.parametrize("a,b,want", [
    ("The garage door is open", "garage door open", True),
    ("Kitchen lights are on", "The garage door is open", False),
    ("a an the", "a an the", False),
    ("", "garage", False),
    ("Washer finished, spin done.", "washer finished spin", True),
    ("alpha bravo charlie delta", "alpha bravo echo foxtrot", False),
    ("alpha bravo charlie", "alpha bravo charlie delta echo", True),
])
def test_messages_similar(og, a, b, want):
    assert og._messages_similar(a, b) is want


def test_similarity_threshold(og):
    assert og._messages_similar("alpha bravo charlie delta", "alpha bravo charlie", 1.0)
    assert not og._messages_similar("alpha bravo charlie delta", "alpha bravo zulu", 0.7)


# ── habituation notes ────────────────────────────────────────────────────────

def test_habit_note(og, load, monkeypatch):
    kw = dict(entity_id="cover.garage", category="security", urgency="low", message="Open.")
    assert og.habit_note(**kw) == "Open. [noted:cover.garage]"
    assert og.habit_note(**kw, habit_key="k") == "Open. [noted:k]"
    assert og.habit_note(**kw, habit_key="") == "Open."
    assert og.habit_note(**{**kw, "category": "safety"}) == "Open."

    def boom(*a, **k):
        raise RuntimeError
    monkeypatch.setattr(load("habituation"), "with_note", boom)
    assert og.habit_note(**kw) == "Open."


# ── recording ────────────────────────────────────────────────────────────────

def test_record_spoken(og):
    _said(og, "Garage open", entity_id="cover.garage", category="security", urgency="medium",
          dedup_key="g", source="core", pushed=True, outcome="spoken", request_id="r1")
    ann = og._STATE.history[-1]
    assert (ann.timestamp, ann.entity_id, ann.category, ann.urgency, ann.message,
            ann.was_spoken) == (og.clock["now"], "cover.garage", "security", "medium",
                                "Garage open", True)
    assert list(og._STATE.recent_messages) == [
        {"timestamp": og.clock["now"], "message": "Garage open", "key": "g"}]
    assert og.hab.recorded == [("cover.garage", "cover.garage")]
    assert og.activity == [{"entity_id": "cover.garage", "category": "security",
                            "urgency": "medium", "message": "Garage open",
                            "was_spoken": True, "source": "core", "pushed": True,
                            "outcome": "spoken", "request_id": "r1"}]


def test_record_defaults_and_unspoken(og):
    _said(og, "Held back", spoken=False)
    assert len(og._STATE.history) == 1 and not og._STATE.recent_messages
    assert og.hab.recorded == []
    assert og.activity[0]["source"] == "observer" and og.activity[0]["pushed"] is False
    assert og.activity[0]["outcome"] == "" and og.activity[0]["request_id"] == ""


def test_record_habit_keys(og):
    _said(og, "One", entity_id="e1", habit_key="custom")
    _said(og, "Two", entity_id="e2", habit_key="")
    _said(og, "Three", entity_id="e3", category="safety")
    assert og.hab.recorded == [("custom", "e1")]


def test_record_survives_failures(og, load, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError
    monkeypatch.setattr(load("habituation"), "record", boom)
    monkeypatch.setattr(load("database"), "save_activity", boom)
    _said(og, "Still logged")
    assert og._STATE.history[-1].message == "Still logged"


# ── shush and unshush ────────────────────────────────────────────────────────

def test_shush_all(og):
    assert og.shush(entity_id="x", all=True) == {"muted_entities": [], "muted_categories": [],
                                                 "all": True}
    assert og._STATE.mute_all is True and og._STATE.muted_entities == set()


def test_shush_targets(og):
    assert og.shush(entity_id="light.a", category="lights") == {
        "muted_entities": ["light.a"], "muted_categories": ["lights"], "all": False}
    _said(og, "First", entity_id="cover.garage")
    _said(og, "Second", entity_id="sensor.held", spoken=False)
    assert og.shush()["muted_entities"] == ["cover.garage"]
    og._STATE.history.clear()
    assert og.shush() == {"muted_entities": [], "muted_categories": [], "all": False}
    assert og.shush(category="info")["muted_entities"] == []


def test_unshush(og):
    og.shush(entity_id="a")
    og.shush(entity_id="b", category="c")
    og.shush(all=True)
    assert og.unshush(entity_id="a", category="zzz") == {"cleared_entities": ["a"],
                                                        "cleared_categories": []}
    assert og.unshush(entity_id="nope") == {"cleared_entities": [], "cleared_categories": []}
    assert og._STATE.mute_all is True
    assert og.unshush() == {"cleared_entities": ["b"], "cleared_categories": ["c"],
                            "blanket_cleared": True}
    assert og._STATE.mute_all is False and not og._STATE.muted_categories
    assert og.unshush(category="c") == {"cleared_entities": [], "cleared_categories": []}


# ── saved mutes ──────────────────────────────────────────────────────────────

class _Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


async def test_save_and_load_mutes(og):
    og.shush(entity_id="z.b")
    og.shush(entity_id="z.a", category="lights")
    og.shush(all=True)
    await og.async_save_mutes(_Hass())
    saved = json.loads(pathlib.Path(og._mutes_path()).read_text())
    assert saved == {"entities": ["z.a", "z.b"], "categories": ["lights"], "all": True}
    og.unshush()
    og.load_mutes()
    assert og._STATE.muted_entities == {"z.a", "z.b"}
    assert og._STATE.muted_categories == {"lights"} and og._STATE.mute_all is True


@pytest.mark.parametrize("content,entities,categories,blanket", [
    ('{"entities": ["a", 3, ""], "categories": "x", "all": "yes"}', {"a"}, set(), False),
    ('{"entities": null, "categories": ["c", null], "all": true}', set(), {"c"}, True),
])
def test_load_cleans_values(og, content, entities, categories, blanket):
    p = pathlib.Path(og._mutes_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    og._STATE.muted_entities.add("old")
    og.load_mutes()
    assert og._STATE.muted_entities == entities
    assert og._STATE.muted_categories == categories and og._STATE.mute_all is blanket


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]", ""])
def test_load_ignores_missing_or_bad_files(og, content):
    p = pathlib.Path(og._mutes_path())
    if content is not None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    og._STATE.muted_entities.add("kept")
    og.load_mutes()
    assert og._STATE.muted_entities == {"kept"}


async def test_save_failure_is_swallowed(og, monkeypatch):
    def boom(data):
        raise OSError("disk full")
    monkeypatch.setattr(og, "_write_mutes", boom)
    await og.async_save_mutes(_Hass())


async def test_saves_never_leave_an_older_state(og):
    og.shush(entity_id="a")
    first = asyncio.ensure_future(og.async_save_mutes(_Hass()))
    og.shush(entity_id="b")
    second = asyncio.ensure_future(og.async_save_mutes(_Hass()))
    await asyncio.gather(first, second)
    assert json.loads(pathlib.Path(og._mutes_path()).read_text())["entities"] == ["a", "b"]


# ── status ───────────────────────────────────────────────────────────────────

def test_status(og):
    assert og.status() == {"announcements_last_hour": 0, "suppressed_last_hour": 0,
                           "muted_entities": [], "muted_categories": [], "mute_all": False,
                           "last_announcement": None}
    _said(og, "Old one")
    og.clock["now"] += 4000
    _said(og, "First")
    og.clock["now"] += 10
    _said(og, "Second")
    _said(og, "Held", spoken=False)
    og.clock["now"] += 5
    og.shush(entity_id="b.x", category="c")
    og.shush(entity_id="a.x")
    st = og.status()
    assert st["announcements_last_hour"] == 2
    assert st["muted_entities"] == ["a.x", "b.x"] and st["muted_categories"] == ["c"]
    assert st["last_announcement"] == {"message": "Second", "seconds_ago": 5}


def test_preserved_quirk_status_never_counts_suppressed(og):
    _said(og, "Held", spoken=False)
    assert og.status()["suppressed_last_hour"] == 0


def test_recent_announcements(og):
    for text in ("one", "two", "three"):
        _said(og, text)
    _said(og, "held", spoken=False)
    assert og.recent_announcements(2) == ["two", "three"]
    assert og.recent_announcements() == ["one", "two", "three"]


def test_recent_within(og):
    _said(og, "old")
    og.clock["now"] += 100
    _said(og, "new")
    _said(og, "quiet", spoken=False)
    assert [a.message for a in og._recent_within(og._STATE.history, 50)] == ["new"]
