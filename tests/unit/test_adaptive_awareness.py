"""Adaptive awareness (opt in): anticipation alerts learn from alerts you mute.

Pins the rules: off by default, needs enough judged alerts, small bounded
steps, anticipation records only (never safety), and an ignore right after an
alert judges that alert "unnecessary".
"""
import datetime
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
        assert dr.set_outcome(rid, v, source="test", db_path=db) is True


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


# ── ignoring something judges the alert ─────────────────────────────────────

def test_ignore_judges_matching_recent_alert(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    rid = dr.record("anticipation_overdue", ref="entity:binary_sensor.porch_door",
                    db_path=db)
    other = dr.record("anticipation_overdue", ref="entity:binary_sensor.garage",
                      db_path=db)
    assert aa.note_ignored("binary_sensor.porch_door", db_path=db) == 1
    assert dr.get(rid, db_path=db)["outcome"] == "unnecessary"
    assert dr.get(rid, db_path=db)["outcome_source"] == "ignore"
    assert dr.get(other, db_path=db)["outcome"] is None


def test_ignore_pattern_can_be_a_glob(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    a = dr.record("anticipation_overdue", ref="entity:binary_sensor.porch_door",
                  db_path=db)
    b = dr.record("anticipation_presence", ref="entity:binary_sensor.porch_back",
                  db_path=db)
    c = dr.record("anticipation_overdue", ref="entity:lock.front", db_path=db)
    assert aa.note_ignored("binary_sensor.porch_*", db_path=db) == 2
    assert dr.get(a, db_path=db)["outcome"] == "unnecessary"
    assert dr.get(b, db_path=db)["outcome"] == "unnecessary"
    assert dr.get(c, db_path=db)["outcome"] is None


def test_ignore_leaves_old_judged_and_other_kinds_alone(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    old = dr.record("anticipation_overdue", ref="entity:lock.front",
                    ts=time.time() - 3 * 86400, db_path=db)
    done = dr.record("anticipation_overdue", ref="entity:lock.front", db_path=db)
    dr.set_outcome(done, "good", source="panel", db_path=db)
    safety = dr.record("intrusion", ref="entity:lock.front", db_path=db)
    assert aa.note_ignored("lock.front", db_path=db) == 0
    assert dr.get(old, db_path=db)["outcome"] is None
    assert dr.get(done, db_path=db)["outcome"] == "good"
    assert dr.get(safety, db_path=db)["outcome"] is None


def test_ignore_records_nothing_when_off(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load, on=False)
    rid = dr.record("anticipation_overdue", ref="entity:lock.front", db_path=db)
    assert aa.note_ignored("lock.front", db_path=db) == 0
    assert dr.get(rid, db_path=db)["outcome"] is None


def test_ignore_with_empty_pattern_judges_nothing(aa, monkeypatch, load, db, dr):
    _enable(monkeypatch, load)
    dr.record("anticipation_overdue", ref="entity:lock.front", db_path=db)
    assert aa.note_ignored("", db_path=db) == 0


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
