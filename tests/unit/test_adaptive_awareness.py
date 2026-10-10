"""Adaptive awareness (opt in): anticipation alerts learn from your ratings.

Pins the rules: off by default, needs enough rated alerts, small bounded
steps, anticipation records only (never safety), and only a confirmation (a
Helpful / Not helpful tap, or the panel) is a verdict. Silence and muting
never are.
"""
import datetime
import sys
import types
import time

import pytest

EID = "binary_sensor.example_morning_activity"
USUAL = 1 * 3600 + 18 * 60          # 01:18


class _Hass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


@pytest.fixture
def aa(load):
    mod = load("adaptive_awareness")
    mod.reset()
    yield mod
    mod.reset()


@pytest.fixture
def dr(load):
    return load("decision_record")


@pytest.fixture
def cog(load):
    c = load("cognition")
    c.reset()
    yield c
    c.reset()


def _enable(monkeypatch, load, on=True):
    nc = load("nova_config")
    monkeypatch.setattr(nc, "get",
                        lambda k, d=None: on if k == "adaptive_awareness" else d)


@pytest.fixture
def db(tmp_path, dr):
    path = str(tmp_path / "decisions.db")
    dr.ensure_schema(path)
    return path


def _judged(dr, db, kind, verdicts, ref=None):
    for v in verdicts:
        rid = dr.record(kind, ref=ref, db_path=db)
        assert dr.set_outcome(rid, v, source="phone", db_path=db) is True  # confirmed source (8.24.0)


# ── the mapping ─────────────────────────────────────────────────────────────

def test_delta_mapping(aa):
    assert aa.delta_from_rate(0.9) == 0.15
    assert aa.delta_from_rate(0.5) == 0.15
    assert aa.delta_from_rate(0.3) == 0.07
    assert aa.delta_from_rate(0.2) == 0.0
    assert aa.delta_from_rate(0.1) == -0.07
    assert aa.delta_from_rate(0.0) == -0.07
    assert aa.delta_from_rate(None) == -0.07
    assert aa.delta_from_rate("junk") == 0.0


# ── off by default ──────────────────────────────────────────────────────────

def test_off_means_no_change(aa, monkeypatch, load):
    _enable(monkeypatch, load, on=False)
    aa._STATE["delta"] = 0.15
    assert aa.current_delta() == 0.0
    assert aa.tolerance_scale() == 1.0
    assert aa.extra_min_days() == 0


async def test_off_does_not_read_the_database(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load, on=False)
    _judged(dr, db, "anticipation_overdue", ["unnecessary"] * 8)
    assert await aa.async_refresh(_Hass(), db_path=db) == 0.0
    assert aa.status()["judged"] == 0


# ── evidence, bounds ────────────────────────────────────────────────────────

async def test_needs_enough_judged_alerts(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    _judged(dr, db, "anticipation_overdue", ["unnecessary"] * 4)
    assert await aa.async_refresh(_Hass(), db_path=db) == 0.0
    assert aa.tolerance_scale() == 1.0


async def test_mostly_unwelcome_is_stricter_and_bounded(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    _judged(dr, db, "anticipation_overdue", ["unnecessary"] * 4)
    _judged(dr, db, "anticipation_presence", ["wrong", "good"])
    assert await aa.async_refresh(_Hass(), db_path=db) == 0.15
    assert aa.tolerance_scale() == pytest.approx(1.45)
    assert aa.extra_min_days() == 3
    st = aa.status()
    assert st["enabled"] and st["judged"] == 6 and st["delta"] == 0.15


async def test_mostly_welcome_is_a_little_looser_never_below_baseline_days(
        aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    _judged(dr, db, "anticipation_routine", ["good"] * 6)
    assert await aa.async_refresh(_Hass(), db_path=db) == -0.07
    assert aa.tolerance_scale() == pytest.approx(0.8)
    assert aa.extra_min_days() == 0


async def test_unrated_alerts_count_for_nothing(aa, monkeypatch, load, db, dr):
    """No guessing: alerts nobody rated never count as welcome, however old."""
    _enable(monkeypatch, load)
    for ts in (time.time() - 2 * 86400, time.time() - 3600):
        for _ in range(9):
            dr.record("anticipation_overdue", ts=ts, db_path=db)
    assert await aa.async_refresh(_Hass(), db_path=db) == 0.0
    assert aa.status()["judged"] == 0


async def test_only_anticipation_records_count(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    # Plenty of unwelcome verdicts, but all on other kinds: no effect.
    _judged(dr, db, "intrusion", ["wrong"] * 8)
    _judged(dr, db, "suggestion", ["unnecessary"] * 8)
    assert await aa.async_refresh(_Hass(), db_path=db) == 0.0
    assert aa.status()["judged"] == 0


async def test_refresh_is_throttled(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    _judged(dr, db, "anticipation_overdue", ["unnecessary"] * 6)
    assert await aa.async_refresh(_Hass(), db_path=db) == 0.15
    _judged(dr, db, "anticipation_overdue", ["good"] * 40)
    assert await aa.async_refresh(_Hass(), db_path=db) == 0.15   # cached


def test_outcome_rate_prefix_matches_family_only(dr, db):
    _judged(dr, db, "anticipation_overdue", ["good", "unnecessary"])
    _judged(dr, db, "anticipation_presence", ["wrong"])
    _judged(dr, db, "suggestion", ["good"] * 5)
    exact = dr.outcome_rate("anticipation", db_path=db)
    fam = dr.outcome_rate("anticipation", None, db, True)
    assert exact["judged"] == 0
    assert fam["judged"] == 3 and fam["unwelcome_rate"] == pytest.approx(2 / 3, abs=1e-3)


# ── confirmations: the rating buttons ───────────────────────────────────────

def test_rating_buttons_only_when_on_and_linked(aa, monkeypatch, load):
    _enable(monkeypatch, load, on=False)
    assert aa.rating_actions(7) == []
    _enable(monkeypatch, load)
    assert aa.rating_actions(None) == []
    assert aa.rating_actions("junk") == []
    assert aa.rating_actions(7) == [
        {"action": "NOVA_AWARE_GOOD_7", "title": "Helpful"},
        {"action": "NOVA_AWARE_BAD_7", "title": "Not helpful"}]


def test_parse_action(aa):
    assert aa.parse_action("NOVA_AWARE_GOOD_12") == (12, True)
    assert aa.parse_action("NOVA_AWARE_BAD_3") == (3, False)
    for junk in (None, "", "NOVA_SLEEP_YES_ab", "NOVA_AWARE_MAYBE_3",
                 "NOVA_AWARE_GOOD_x", "NOVA_AWARE_GOOD_"):
        assert aa.parse_action(junk) is None


def test_tap_records_the_verdict_once(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    good = dr.record("anticipation_overdue", db_path=db)
    bad = dr.record("anticipation_presence", db_path=db)
    assert aa.record_rating(good, True, db_path=db) is True
    assert aa.record_rating(bad, False, db_path=db) is True
    assert dr.get(good, db_path=db)["outcome"] == "good"
    assert dr.get(good, db_path=db)["outcome_source"] == "phone"
    assert dr.get(bad, db_path=db)["outcome"] == "unnecessary"
    # The first verdict stands.
    assert aa.record_rating(good, False, db_path=db) is False
    assert dr.get(good, db_path=db)["outcome"] == "good"


def test_tap_cannot_judge_other_kinds_or_while_off(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    safety = dr.record("intrusion", db_path=db)
    assert aa.record_rating(safety, False, db_path=db) is False
    assert dr.get(safety, db_path=db)["outcome"] is None
    assert aa.record_rating(999, True, db_path=db) is False
    _enable(monkeypatch, load, on=False)
    rid = dr.record("anticipation_overdue", db_path=db)
    assert aa.record_rating(rid, True, db_path=db) is False
    assert dr.get(rid, db_path=db)["outcome"] is None


async def test_rated_alerts_move_the_adjustment(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    for _ in range(6):
        aa.record_rating(dr.record("anticipation_routine", db_path=db), True, db_path=db)
    assert await aa.async_refresh(_Hass(), db_path=db) == -0.07


async def test_listener_stores_a_tap_and_refreshes(aa, monkeypatch, load, db, dr, fake_hass):
    _enable(monkeypatch, load)
    rid = dr.record("anticipation_overdue", db_path=db)
    stored = []
    monkeypatch.setattr(aa, "record_rating",
                        lambda r, h: stored.append((r, h)) or True)
    aa._STATE["ts"] = time.time()
    listeners = {}
    monkeypatch.setattr(fake_hass.bus, "async_listen",
                        lambda ev, fn: listeners.setdefault(ev, fn) and (lambda: None))
    aa.async_listen(fake_hass)
    handler = listeners["mobile_app_notification_action"]
    handler(type("E", (), {"data": {"action": "NOVA_SLEEP_YES_ab"}})())
    handler(type("E", (), {"data": {"action": f"NOVA_AWARE_BAD_{rid}"}})())
    await fake_hass.drain()
    assert stored == [(rid, False)]
    assert aa._STATE["ts"] == 0.0


async def test_rating_prompt_is_silent_and_carries_the_buttons(aa, monkeypatch, load, fake_hass):
    nt = load("notify_targets")
    sent = []

    async def _send(hass, config, payload, **kw):
        sent.append((payload, kw))
        return ["notify.phone"]
    monkeypatch.setattr(nt, "async_send_configured_notifications", _send)
    _enable(monkeypatch, load, on=False)
    assert await aa.async_send_rating_prompt(fake_hass, {}, "msg", 5) is False
    _enable(monkeypatch, load)
    assert await aa.async_send_rating_prompt(fake_hass, {}, "msg", None) is False
    assert await aa.async_send_rating_prompt(fake_hass, {}, "Porch usually opens by now", 5)
    payload, kw = sent[-1]
    assert payload["title"] == "Was this helpful?"
    assert payload["message"] == "Porch usually opens by now"
    assert [a["action"] for a in payload["data"]["actions"]] == [
        "NOVA_AWARE_GOOD_5", "NOVA_AWARE_BAD_5"]
    assert payload["data"]["push"] == {"interruption-level": "passive"}
    assert payload["data"]["importance"] == "low"
    assert len(sent) == 1


def test_ignore_tool_no_longer_judges_anything(load):
    import inspect
    mem = load("agent_runtime.capabilities.memory")
    assert "adaptive_awareness" not in inspect.getsource(mem._exec_ignore)
    assert not hasattr(load("adaptive_awareness"), "note_ignored")


# ── the alerts themselves ───────────────────────────────────────────────────

def _at(hour, minute=0):
    base = datetime.datetime.now().replace(hour=hour, minute=minute, second=0,
                                           microsecond=0)
    return base.timestamp()


def _learn(cog, now, days):
    today = cog._local_day(now)
    e = cog._Entry(now - 20 * 86400)
    for d in range(today - days, today):
        e.daily_first.append((d, USUAL))
    e.last_first_day = today - 1
    cog._MODEL[EID] = e
    cog._OBSERVING_SINCE = now - 86400


def _hass(fake_hass):
    fake_hass.states.set(EID, "on", friendly_name="Sun Solar rising")
    return fake_hass


def test_overdue_alert_waits_longer_when_awareness_says_unwelcome(
        cog, aa, monkeypatch, load, fake_hass):
    now = _at(2, 0)                     # usual 01:18, base grace 30 minutes
    _learn(cog, now, 10)
    assert len(cog.predict_overdue(_hass(fake_hass), now)) == 1

    cog.reset()
    _learn(cog, now, 10)
    _enable(monkeypatch, load)
    aa._STATE["delta"] = 0.15           # grace becomes about 43 minutes
    assert cog.predict_overdue(_hass(fake_hass), now) == []
    assert cog.predict_overdue(_hass(fake_hass), _at(2, 5)) != []


def test_routine_needs_more_days_when_awareness_says_unwelcome(
        cog, aa, monkeypatch, load):
    now = _at(3, 0)
    _learn(cog, now, 8)
    entry = cog._MODEL[EID]
    assert cog._routine_of(entry) is not None
    _enable(monkeypatch, load)
    aa._STATE["delta"] = 0.15           # needs 7 + 3 = 10 days
    assert cog._routine_of(entry) is None


def test_overdue_alert_links_to_its_entity(cog, dr, db, fake_hass, monkeypatch):
    now = _at(3, 0)
    _learn(cog, now, 10)
    monkeypatch.setattr(dr, "_resolve", lambda p: db)
    assert len(cog.predict_overdue(_hass(fake_hass), now)) == 1
    rows = dr.recent(kind="anticipation_overdue", db_path=db)
    assert rows and rows[0]["ref"] == f"entity:{EID}"


def test_overdue_alert_carries_its_decision_id(cog, dr, db, fake_hass, monkeypatch):
    now = _at(3, 0)
    _learn(cog, now, 10)
    monkeypatch.setattr(dr, "_resolve", lambda p: db)
    out = cog.predict_overdue(_hass(fake_hass), now)
    rows = dr.recent(kind="anticipation_overdue", db_path=db)
    assert out[0]["decision_id"] == rows[0]["id"]


# ── routing: every rated alert reaches the phone with the buttons ──────────

@pytest.fixture
def routed(load, monkeypatch, aa):
    cc = load("cognitive_core")
    hab = load("habituation")
    monkeypatch.setattr(hab, "is_quiet", lambda k: False)
    monkeypatch.setattr(hab, "record", lambda *a, **k: None)
    pushes, prompts, spoken = [], [], []

    async def _push(hass, config, message, action_type, snap=None, *,
                    request_id=None, extra_data=None):
        pushes.append(extra_data)
    monkeypatch.setattr(cc, "_push_notification", _push)

    async def _prompt(hass, config, message, decision_id):
        prompts.append((message, decision_id))
        return True
    monkeypatch.setattr(aa, "async_send_rating_prompt", _prompt)

    tts = types.ModuleType("jc.tts_helper")
    tts.resolve_tts_for_context = lambda *a, **k: "tts.x"

    async def _announce(hass, message, *a, **k):
        spoken.append(message)
    tts.async_announce = _announce
    ar = types.ModuleType("jc.audio_routing")
    ar.observer_speak_target = lambda *a, **k: (["media_player.x"], "normal")
    monkeypatch.setitem(sys.modules, "jc.tts_helper", tts)
    monkeypatch.setitem(sys.modules, "jc.audio_routing", ar)
    sd = types.ModuleType("jc.sleep_detection")
    sd._in_quiet_hours = lambda *a: False
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sd)
    return cc, pushes, prompts, spoken


def _alert(**kw):
    a = {"type": "anticipation_overdue", "urgency": "low",
         "message": "The porch usually has activity by now.",
         "pattern_key": "overdue:binary_sensor.porch", "decision_id": 42}
    a.update(kw)
    return a


async def test_spoken_alert_gets_a_silent_rating_prompt(routed, fake_hass, monkeypatch, load):
    cc, pushes, prompts, spoken = routed
    _enable(monkeypatch, load)
    await cc._emit_action(fake_hass, {}, _alert(), sleeping=False)
    assert spoken and prompts == [("The porch usually has activity by now.", 42)]
    assert pushes == []


async def test_alert_pushed_in_quiet_hours_carries_the_buttons(routed, fake_hass, monkeypatch, load):
    cc, pushes, prompts, spoken = routed
    _enable(monkeypatch, load)
    await cc._emit_action(fake_hass, {}, _alert(), sleeping=True)
    assert not spoken and prompts == []
    assert [a["action"] for a in pushes[0]["actions"]] == [
        "NOVA_AWARE_GOOD_42", "NOVA_AWARE_BAD_42"]


async def test_no_buttons_without_a_record_or_while_off(routed, fake_hass, monkeypatch, load):
    cc, pushes, prompts, spoken = routed
    _enable(monkeypatch, load)
    await cc._emit_action(fake_hass, {}, _alert(decision_id=None), sleeping=True)
    assert pushes == [{}]
    _enable(monkeypatch, load, on=False)
    await cc._emit_action(fake_hass, {}, _alert(pattern_key="overdue:other"), sleeping=True)
    assert pushes[-1] == {}
