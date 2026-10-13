"""8.46.0 (design D1): one resolver each for presence, sleep and quiet hours
(world.py), a fresh reading per tick, event or request, and only the facts a
decision used kept with it.

* The resolvers: each answer says when it was read and where it came from.
* Nothing is cached: a change between two readings is always seen.
* Stale readings: an Observer event reads afresh even while a tick's
  snapshot is held, and delivery judges an event against the reading the
  event took, never a second one.
* Presence changes: a person leaving, a linked phone tracker, a person who
  cannot be read.
* A decision record keeps only household, asleep and quiet hours, each with
  its source and time, not the whole snapshot.

All state is fake.
"""
import time

import pytest


@pytest.fixture
def world(load):
    w = load("world")
    w.end_tick()
    yield w
    w.end_tick()


@pytest.fixture
def awake(load, monkeypatch):
    """sleep_detection answers from a switch; quiet hours from a switch."""
    sd = load("sleep_detection")
    sw = {"asleep": False, "quiet": False, "sleep_kw": []}

    def is_sleeping(hass, **kw):
        sw["sleep_kw"].append(kw)
        return sw["asleep"], "bedroom occupied in quiet hours" if sw["asleep"] else "awake"
    monkeypatch.setattr(sd, "is_sleeping", is_sleeping)
    monkeypatch.setattr(sd, "_in_quiet_hours", lambda start, end: sw["quiet"])
    return sw


# ── the resolvers ───────────────────────────────────────────────────────────

def test_quiet_hours_use_the_live_setting_and_say_so(world, fake_hass, awake, nova_runtime):
    nova_runtime(fake_hass, {"observer_quiet_start": "23:00", "observer_quiet_end": "06:30"})
    awake["quiet"] = True
    before = time.time()
    fact = world.quiet_hours(fake_hass)
    assert fact.value is True
    assert fact.source == "quiet hours setting 23:00-06:30"
    assert before <= fact.read_at <= time.time()


def test_sleep_uses_the_live_bedrooms_and_window(world, fake_hass, awake, nova_runtime):
    nova_runtime(fake_hass, {"bedroom_areas": ["bedroom"], "observer_quiet_start": "23:00",
                             "observer_quiet_end": "06:30"})
    awake["asleep"] = True
    fact = world.asleep(fake_hass)
    assert fact.value is True and fact.source == "bedroom occupied in quiet hours"
    assert awake["sleep_kw"][-1] == {"bedroom_area_ids": ["bedroom"],
                                     "quiet_start": "23:00", "quiet_end": "06:30"}


def test_a_resolver_that_fails_is_unknown_never_false(world, fake_hass, load, monkeypatch):
    sd = load("sleep_detection")

    def boom(*a, **k):
        raise RuntimeError("no sensors")
    monkeypatch.setattr(sd, "is_sleeping", boom)
    monkeypatch.setattr(sd, "_in_quiet_hours", boom)
    assert world.asleep(fake_hass).value == world.UNKNOWN
    assert world.quiet_hours(fake_hass).value == world.UNKNOWN


def test_presence_says_which_reading_decided_it(world, fake_hass):
    fake_hass.states.set("person.a", "not_home", device_trackers=["device_tracker.a_phone"])
    fake_hass.states.set("device_tracker.a_phone", "home")
    fake_hass.states.set("person.b", "not_home")
    fact = world.presence(fake_hass)
    assert fact.value.residents == "home"
    assert fact.source == "people: person.a home via device_tracker.a_phone, person.b away"
    assert fact.read_at > 0


def test_a_person_who_cannot_be_read_is_unknown_never_away(world, fake_hass):
    fake_hass.states.set("person.a", "unavailable")
    fact = world.presence(fake_hass)
    assert fact.value.residents == "unknown"
    assert fact.source == "people: person.a unknown"


def test_the_tick_delivery_and_the_observer_share_one_quiet_window(
        load, fake_hass, awake, nova_runtime):
    """Before 8.46.0 the tick fell back to 22:00 from its own config while
    delivery read the live setting. Now every reader gets the same window."""
    nova_runtime(fake_hass, {"observer_quiet_start": "21:15", "observer_quiet_end": "06:45"})
    w, dl = load("world"), load("delivery")
    assert w.quiet_window(fake_hass) == ("21:15", "06:45")
    assert dl.quiet_hours(fake_hass) == ("21:15", "06:45")
    w.asleep(fake_hass)
    assert awake["sleep_kw"][-1]["quiet_start"] == "21:15"


# ── nothing is cached ───────────────────────────────────────────────────────

def test_a_presence_change_is_seen_by_the_next_reading(world, fake_hass, load):
    ap = load("alert_path")
    fake_hass.states.set("person.a", "home")
    assert ap.situation(fake_hass).residents == "home"
    fake_hass.states.set("person.a", "not_home")
    assert ap.situation(fake_hass).residents == "away"
    assert world.presence(fake_hass).value.residents == "away"


def test_delivery_reads_afresh_for_each_request(load, fake_hass, awake, monkeypatch):
    dl = load("delivery")
    og = load("output_gate")
    monkeypatch.setattr(og, "_STATE", og.GateState())
    req = dl.DeliveryRequest(source="test", message="hi", urgency="medium")
    fake_hass.states.set("person.a", "home")
    assert dl.conditions(fake_hass, req).anyone_home is True
    fake_hass.states.set("person.a", "not_home")
    assert dl.conditions(fake_hass, req).anyone_home is False
    awake["asleep"] = True
    assert dl.conditions(fake_hass, req).asleep is True


# ── stale readings ──────────────────────────────────────────────────────────

def test_an_event_reads_afresh_while_a_tick_holds_its_snapshot(world, fake_hass, load):
    """A tick's snapshot is the same for every source in that tick. An
    Observer event is its own decision, so it reads presence now."""
    household = load("household")
    fake_hass.states.set("person.a", "home")
    world.begin_tick(fake_hass, {})
    fake_hass.states.set("person.a", "not_home")
    assert household.snapshot(fake_hass).residents == "home"        # the tick's reading
    assert world.presence(fake_hass).value.residents == "away"      # the event's reading


def test_delivery_judges_an_event_against_the_reading_it_took(load, fake_hass, awake,
                                                               monkeypatch):
    dl, ap, w = load("delivery"), load("alert_path"), load("world")
    og = load("output_gate")
    monkeypatch.setattr(og, "_STATE", og.GateState())
    fake_hass.states.set("person.a", "home")
    sit = ap.situation(fake_hass, sleeping=w.Fact(True, "event"), quiet=w.Fact(False, "event"))
    # The house changes after the event was read; nothing is read again.
    fake_hass.states.set("person.a", "not_home")

    def no_second_reading(*a, **k):
        raise AssertionError("read again")
    monkeypatch.setattr(w, "asleep", no_second_reading)
    monkeypatch.setattr(w, "quiet_hours", no_second_reading)
    monkeypatch.setattr(w, "presence", no_second_reading)
    cond = dl.conditions(fake_hass, dl.DeliveryRequest(
        source="observer", message="hi", urgency="medium", reading=sit))
    assert cond.asleep is True and cond.quiet_hours is False
    assert cond.anyone_home is True and cond.situation is sit


async def test_the_record_keeps_the_reading_the_decision_used(load, fake_hass, awake,
                                                               monkeypatch):
    dl, ap = load("delivery"), load("alert_path")
    saved = []
    monkeypatch.setattr(ap, "async_record_decision",
                        lambda hass, kind, **kw: _done(saved.append((kind, kw))))
    fake_hass.states.set("person.a", "home")
    sit = ap.situation(fake_hass, sleeping=False, quiet=False)
    fake_hass.states.set("person.a", "not_home")
    cond = dl.Conditions(situation=sit)
    req = dl.DeliveryRequest(source="sentinel", message="m", record_kind="left_open")
    await dl._record(fake_hass, req, cond, dl.Outcome(dl.Decision(True, False)))
    (_kind, kw), = saved
    assert kw["sit"] is sit and kw["sit"].residents == "home"


async def _done(_value):
    return None


# ── the facts a decision keeps ──────────────────────────────────────────────

def test_a_decision_record_keeps_only_the_facts_it_used(load, fake_hass, awake,
                                                        nova_runtime):
    ap = load("alert_path")
    nova_runtime(fake_hass, {"observer_quiet_start": "22:00", "observer_quiet_end": "07:00"})
    fake_hass.states.set("person.a", "not_home", device_trackers=["device_tracker.a_phone"])
    fake_hass.states.set("device_tracker.a_phone", "home")
    fake_hass.states.set("lock.front", "unlocked")         # not used by this decision
    sit = ap.situation(fake_hass)
    obs = ap.decision_fields("left_open", source="sentinel", sit=sit)["observation"]
    assert obs["residents"] == "home" and obs["asleep"] is False
    read = obs["read"]
    assert set(read) == {"household", "asleep", "quiet_hours"}
    assert read["household"]["value"] == {"residents": "home", "posture": "unknown"}
    assert read["household"]["source"] == "people: person.a home via device_tracker.a_phone"
    assert read["asleep"] == {"value": False, "source": "awake",
                              "read_at": read["asleep"]["read_at"]}
    assert read["quiet_hours"]["source"] == "quiet hours setting 22:00-07:00"
    assert all(f["read_at"] for f in read.values())
    assert "lock.front" not in str(obs)


def test_a_reading_given_by_the_decision_says_so(load, fake_hass):
    ap = load("alert_path")
    sit = ap.situation(fake_hass, sleeping=True, quiet=False)
    assert sit.sleeping is True
    assert sit.read["asleep"]["source"] == "the decision's own reading"


def test_world_facts_returns_only_what_is_named(world, fake_hass, awake):
    fake_hass.states.set("person.a", "home")
    snap = world.read(fake_hass, {}, asleep=world.asleep(fake_hass),
                      quiet_hours=world.quiet_hours(fake_hass))
    facts = snap.facts("residents", "asleep")
    assert set(facts) == {"residents", "asleep"}
    assert facts["residents"]["value"] == "home"
    assert facts["residents"]["source"] == "people: person.a home"
    assert facts["asleep"]["source"] == "awake"


def test_a_reading_is_frozen(load, fake_hass):
    ap = load("alert_path")
    sit = ap.situation(fake_hass, sleeping=False, quiet=False)
    with pytest.raises(Exception):
        sit.sleeping = True
