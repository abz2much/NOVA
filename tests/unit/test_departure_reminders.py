"""Departure reminders learned from real presence sampling.

Every scenario drives cognition.sample_presence() every 15 minutes over
simulated weeks (the way the cognitive loop does), so the learned model is
exactly what Nova builds, then checks cognition.predict_presence().
"""
import datetime as dt

import pytest

from fakes import FakeHass

MONDAY = dt.datetime(2026, 8, 31)          # four full weeks before the checks


@pytest.fixture
def cog(load, monkeypatch):
    c = load("cognition")
    c.reset()
    jc = load("nova_config")
    cfg: dict = {}
    monkeypatch.setattr(jc, "get", lambda k, d=None: cfg.get(k, d))
    c._test_cfg = cfg
    yield c
    c.reset()


def _utc(t):
    return t.astimezone(dt.timezone.utc)


class House:
    def __init__(self, cog, entity="person.abi", name="Abi"):
        self.cog = cog
        self.hass = FakeHass()
        self.entity = entity
        self.name = name
        self.last = ("home", MONDAY - dt.timedelta(days=1))
        self._set()

    def _set(self):
        self.hass.states.set(self.entity, self.last[0], last_changed=_utc(self.last[1]),
                             friendly_name=self.name)

    def run(self, start, end, schedule):
        """Sample every 15 minutes from start to end; schedule(date) gives
        that day's (hour, minute, state) changes."""
        t = start
        while t < end:
            day = t.replace(hour=0, minute=0)
            for h, m, s in sorted(schedule(day)):
                at = day.replace(hour=h, minute=m)
                if self.last[1] < at <= t:
                    self.last = (s, at)
            self._set()
            self.cog.sample_presence(self.hass, t.timestamp())
            t += dt.timedelta(minutes=15)

    def check(self, when):
        return [o["message"] for o in self.cog.predict_presence(self.hass, when.timestamp())]


def school_run(day):
    return [(12, 0, "not_home"), (15, 0, "home")] if day.weekday() < 5 else []


def two_trips(day):
    if day.weekday() >= 5:
        return []
    return [(9, 0, "not_home"), (10, 0, "home"), (12, 0, "not_home"), (15, 0, "home")]


def _learn(cog, schedule, days=26, **kw):
    house = House(cog, **kw)
    house.run(MONDAY, MONDAY + dt.timedelta(days=days), schedule)
    return house


def test_no_reminder_on_saturday_for_a_weekday_routine(cog):
    """B1: 26 days of weekday school runs end on a Friday; Saturday is quiet."""
    house = _learn(cog, school_run)
    sat = MONDAY + dt.timedelta(days=26)
    assert sat.weekday() == 5
    house.run(sat, sat.replace(hour=13, minute=1), school_run)
    for hh, mm in [(11, 45), (12, 0), (12, 31), (13, 0)]:
        assert house.check(sat.replace(hour=hh, minute=mm)) == [], (hh, mm)


def test_reminder_arrives_at_the_lead_time_before_the_usual_time(cog):
    """B2: default lead 15 minutes, before the usual 12:00, not after."""
    house = _learn(cog, school_run, days=28)
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=11, minute=44), school_run)
    assert house.check(mon.replace(hour=11, minute=44)) == []
    assert house.check(mon.replace(hour=11, minute=45)) == [
        "You usually leave around 12:00. It's 11:45 now."]
    assert house.check(mon.replace(hour=11, minute=50)) == []      # once a day


def test_lead_time_is_configurable(cog):
    cog._test_cfg["routine_departure_lead_minutes"] = 30
    house = _learn(cog, school_run, days=28)
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=11, minute=29), school_run)
    assert house.check(mon.replace(hour=11, minute=29)) == []
    assert house.check(mon.replace(hour=11, minute=30)) == [
        "You usually leave around 12:00. It's 11:30 now."]


def test_still_home_check_follows_later(cog):
    house = _learn(cog, school_run, days=28)
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=12, minute=31), lambda d: [])   # stays home
    assert house.check(mon.replace(hour=11, minute=45))
    assert house.check(mon.replace(hour=12, minute=29)) == []
    assert house.check(mon.replace(hour=12, minute=31)) == [
        "You're usually out by 12:00 and you're still home."]


def test_no_reminder_once_left(cog):
    house = _learn(cog, school_run, days=28)
    mon = MONDAY + dt.timedelta(days=28)
    leave_early = lambda d: [(11, 40, "not_home")]               # noqa: E731
    house.run(mon, mon.replace(hour=12, minute=45), leave_early)
    assert house.check(mon.replace(hour=11, minute=45)) == []
    assert house.check(mon.replace(hour=12, minute=40)) == []


def test_second_regular_departure_is_learned(cog):
    """B3: a 09:00 errand before the 12:00 school run every weekday."""
    house = _learn(cog, two_trips, days=28)
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=8, minute=45), two_trips)
    assert house.check(mon.replace(hour=8, minute=45)) == [
        "You usually leave around 09:00. It's 08:45 now."]
    house.run(mon.replace(hour=8, minute=45), mon.replace(hour=11, minute=45), two_trips)
    assert house.check(mon.replace(hour=11, minute=45)) == [
        "You usually leave around 12:00. It's 11:45 now."]


def test_tracker_is_not_a_person(cog):
    """B4: with person entities present, a device tracker's routine is ignored."""
    house = _learn(cog, school_run, days=28, entity="device_tracker.home_cloud",
                   name="Home Cloud")
    house.hass.states.set("person.abi", "home", friendly_name="Abi")
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=12, minute=45), lambda d: [])
    assert house.check(mon.replace(hour=11, minute=45)) == []
    assert house.check(mon.replace(hour=12, minute=40)) == []


def test_trackers_used_when_the_household_has_no_person_entities(cog):
    house = _learn(cog, school_run, days=28, entity="device_tracker.abi_phone",
                   name="Abi's Phone")
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=11, minute=45), lambda d: [])
    assert house.check(mon.replace(hour=11, minute=45)) == [
        "You usually leave around 12:00. It's 11:45 now."]


def test_named_when_someone_else_is_home(cog):
    house = _learn(cog, school_run, days=28)
    house.hass.states.set("person.sam", "home", friendly_name="Sam")
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=11, minute=45), lambda d: [])
    assert house.check(mon.replace(hour=11, minute=45)) == [
        "Abi, you usually leave around 12:00. It's 11:45 now."]


def test_quiet_after_three_missed_days_of_that_type(cog):
    """School holidays: three weekdays at home and the reminder stops."""
    house = _learn(cog, school_run, days=28)
    mon = MONDAY + dt.timedelta(days=28)
    thu = mon + dt.timedelta(days=3)
    house.run(mon, thu.replace(hour=11, minute=45), lambda d: [])
    assert house.check(thu.replace(hour=11, minute=45)) == []


def test_reminder_once_a_day_across_restart(cog, tmp_path):
    house = _learn(cog, school_run, days=28)
    mon = MONDAY + dt.timedelta(days=28)
    house.run(mon, mon.replace(hour=11, minute=45), lambda d: [])
    assert house.check(mon.replace(hour=11, minute=45))
    db = str(tmp_path / "patterns.db")
    now = mon.replace(hour=11, minute=46).timestamp()
    cog.save_to_db(db, now)
    cog.reset()
    cog.load_from_db(db, now)
    assert house.check(mon.replace(hour=11, minute=50)) == []


def test_history_saved_before_this_change_is_reused(cog, tmp_path):
    """Models saved by v7.124.1 hold first departures as (day ordinal,
    seconds); the weekday comes from the ordinal, so no new wait."""
    import json
    import sqlite3
    days = [MONDAY + dt.timedelta(days=i) for i in range(26)]
    old = {"last_state": "home", "last_changed": MONDAY.timestamp(),
           "first_seen": MONDAY.timestamp(), "transitions": [], "n": 0, "mean": 0.0,
           "m2": 0.0, "hours": [0] * 24, "occ": {}, "daily_first": [],
           "last_first_day": 0,
           "depart_first": [[d.toordinal(), 43200] for d in days if d.weekday() < 5],
           "return_first": [], "pres_depart_day": 0, "pres_return_day": 0}
    db = str(tmp_path / "patterns.db")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cognition_model (entity_id TEXT PRIMARY KEY, "
                     "data TEXT, updated REAL)")
        conn.execute("INSERT INTO cognition_model VALUES (?, ?, 0)",
                     ("person.abi", json.dumps(old)))
    cog.load_from_db(db, MONDAY.timestamp())
    house = House(cog)
    mon = MONDAY + dt.timedelta(days=28)
    sat = MONDAY + dt.timedelta(days=26)
    house.hass.states.set("person.abi", "home", last_changed=_utc(sat), friendly_name="Abi")
    assert house.check(sat.replace(hour=11, minute=45)) == []
    assert house.check(mon.replace(hour=11, minute=45)) == [
        "You usually leave around 12:00. It's 11:45 now."]
