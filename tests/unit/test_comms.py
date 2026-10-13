"""Acceptance tests for comms.py (calendar agent), written from its behaviour spec."""
from datetime import datetime, timedelta

import pytest



@pytest.fixture
def cm(load):
    return load("comms")


@pytest.fixture
def cfg(load, monkeypatch):
    store = {}
    monkeypatch.setattr(load("nova_config"), "get", lambda key, default=None: store.get(key, default))
    return store


def clock(dt):
    """12-hour clock, no leading zero on the hour."""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def ev(title, start, end, all_day=False):
    return {"title": title, "start": start, "end": end, "all_day": all_day}


T0 = datetime(2026, 3, 2, 9, 0)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


# ── _parse ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("2026-03-02", datetime(2026, 3, 2)),
    ("2026-03-02T08:15:30", datetime(2026, 3, 2, 8, 15, 30)),
    ("2026-03-02 08:15:30", datetime(2026, 3, 2, 8, 15, 30)),
    ("2026-03-02T08:15", datetime(2026, 3, 2, 8, 15)),
    ("  2026-03-02  ", datetime(2026, 3, 2)),
    ("2026-03-02 later", datetime(2026, 3, 2)),
])
def test_parse_valid(cm, raw, expected):
    got = cm._parse(raw)
    assert got == expected and got.tzinfo is None


@pytest.mark.parametrize("raw", [None, "", "nonsense", "2026-13-40", "T", "12:00 noon"])
def test_parse_invalid(cm, raw):
    assert cm._parse(raw) is None


@pytest.fixture
def ha_zone(monkeypatch):
    """Home Assistant's zone set to UTC+1, unlike the test machine's."""
    import sys
    from datetime import timezone
    zone = timezone(timedelta(hours=1))
    dt = sys.modules["homeassistant.util.dt"]
    monkeypatch.setattr(dt, "as_local", lambda v: v.astimezone(zone))
    return zone


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-02T08:15:30Z", datetime(2026, 3, 2, 9, 15, 30)),
    ("2026-03-02T08:15:30+00:00", datetime(2026, 3, 2, 9, 15, 30)),
    ("2026-03-02T08:15:30+02:00", datetime(2026, 3, 2, 7, 15, 30)),
    ("2026-03-02T08:15:30-07:00", datetime(2026, 3, 2, 16, 15, 30)),
    ("2026-03-02T08:15:30.987654+0000", datetime(2026, 3, 2, 9, 15, 30)),
    ("2026-03-02 08:15:30", datetime(2026, 3, 2, 8, 15, 30)),
])
def test_parse_converts_offsets_to_ha_local_time(cm, ha_zone, raw, expected):
    # Regression (8.49.2): offsets were dropped, so a UTC time read an hour out.
    got = cm._parse(raw)
    assert got == expected and got.tzinfo is None


def test_agenda_compares_with_ha_local_now(cm, cfg, fake_hass, ha_zone, monkeypatch):
    from datetime import timezone
    local_now = datetime.now(timezone.utc).astimezone(ha_zone).replace(tzinfo=None)
    soon = local_now + timedelta(minutes=30)
    events = [ev("Soon", soon, soon + timedelta(minutes=10))]
    monkeypatch.setattr(cm, "gather_events", lambda hass: list(events))
    assert cm.agenda(fake_hass, 1)["count"] == 1
    later = local_now + timedelta(minutes=90)
    events[:] = [ev("Later", later, later + timedelta(minutes=10))]
    assert cm.agenda(fake_hass, 1)["count"] == 0


# ── gather_events ────────────────────────────────────────────────────────────

def test_gather_none_hass(cm):
    assert cm.gather_events(None) == []


def test_gather_broken_states(cm):
    class Broken:
        class states:
            @staticmethod
            def async_all(domain=None):
                raise RuntimeError("x")
    assert cm.gather_events(Broken()) == []


def test_gather_shapes_events(cm, fake_hass):
    fake_hass.states.set("calendar.work", "on", message="Standup", friendly_name="Work",
                         start_time="2026-03-02 09:00:00", end_time="2026-03-02 09:15:00",
                         all_day=False, location="Room 1")
    fake_hass.states.set("calendar.home", "off", friendly_name="Home",
                         start_time="2026-03-03", end_time="2026-03-04", all_day=True)
    fake_hass.states.set("calendar.misc", "off", start_time="2026-03-05T10:00:00")
    fake_hass.states.set("calendar.empty", "off", friendly_name="Nothing")
    fake_hass.states.set("calendar.bad", "off", message="X", start_time="garbage")
    fake_hass.states.set("light.hall", "on", message="not a calendar",
                         start_time="2026-03-02 09:00:00")
    out = cm.gather_events(fake_hass)
    assert [e["calendar"] for e in out] == ["calendar.work", "calendar.home", "calendar.misc"]
    work, home, misc = out
    assert work == {"calendar": "calendar.work", "title": "Standup",
                    "start": datetime(2026, 3, 2, 9), "end": datetime(2026, 3, 2, 9, 15),
                    "all_day": False, "location": "Room 1", "active": True}
    assert home["title"] == "Home" and home["all_day"] is True
    assert home["start"] == datetime(2026, 3, 3) and home["end"] == datetime(2026, 3, 4)
    assert home["active"] is False and home["location"] is None
    assert misc["title"] == "calendar.misc"
    assert misc["end"] == datetime(2026, 3, 5, 11)


def test_gather_bad_end_defaults_one_hour(cm, fake_hass):
    fake_hass.states.set("calendar.x", "on", message="Call",
                         start_time="2026-03-02T14:00:00", end_time="later")
    assert cm.gather_events(fake_hass)[0]["end"] == datetime(2026, 3, 2, 15)


def test_gather_title_is_string(cm, fake_hass):
    fake_hass.states.set("calendar.x", "on", message=42, start_time="2026-03-02")
    assert cm.gather_events(fake_hass)[0]["title"] == "42"


# ── find_conflicts ───────────────────────────────────────────────────────────

def test_no_conflicts(cm):
    a, b = ev("A", at(0), at(30)), ev("B", at(60), at(90))
    assert cm.find_conflicts([a, b], 15) == {"overlaps": [], "tight": []}


def test_overlap_pairs_are_ordered_by_start(cm):
    late = ev("Late", at(20), at(50))
    early = ev("Early", at(0), at(30))
    r = cm.find_conflicts([late, early], 15)
    assert r["overlaps"] == [(early, late)]
    assert r["overlaps"][0][0] is early
    assert r["tight"] == []


def test_every_overlap_reported(cm):
    a = ev("A", at(0), at(120))
    b = ev("B", at(10), at(20))
    c = ev("C", at(30), at(40))
    r = cm.find_conflicts([a, b, c], 15)
    assert r["overlaps"] == [(a, b), (a, c)]
    assert r["tight"] == [(b, c)]


def test_tight_transition(cm):
    a, b = ev("A", at(0), at(30)), ev("B", at(40), at(60))
    assert cm.find_conflicts([a, b], 15) == {"overlaps": [], "tight": [(a, b)]}


def test_back_to_back_is_tight_not_overlap(cm):
    a, b = ev("A", at(0), at(30)), ev("B", at(30), at(60))
    assert cm.find_conflicts([a, b], 15) == {"overlaps": [], "tight": [(a, b)]}


def test_gap_equal_to_threshold_not_tight(cm):
    a, b = ev("A", at(0), at(30)), ev("B", at(45), at(60))
    assert cm.find_conflicts([a, b], 15)["tight"] == []


def test_only_first_following_event_checked_for_tightness(cm):
    a = ev("A", at(0), at(30))
    b = ev("B", at(40), at(45))
    c = ev("C", at(41), at(50))
    r = cm.find_conflicts([a, b, c], 15)
    assert r["tight"] == [(a, b)]
    assert r["overlaps"] == [(b, c)]


def test_all_day_ignored(cm):
    a = ev("Holiday", at(0), at(24 * 60), all_day=True)
    b = ev("B", at(10), at(20))
    assert cm.find_conflicts([a, b], 15) == {"overlaps": [], "tight": []}


@pytest.mark.parametrize("gap", [0, -5])
def test_non_positive_gap_never_tight(cm, gap):
    a, b = ev("A", at(0), at(30)), ev("B", at(30), at(60))
    assert cm.find_conflicts([a, b], gap)["tight"] == []


def test_gap_string_converted(cm):
    a, b = ev("A", at(0), at(30)), ev("B", at(35), at(60))
    assert cm.find_conflicts([a, b], "10")["tight"] == [(a, b)]


def test_find_conflicts_raises_on_bad_input(cm):
    with pytest.raises(Exception):
        cm.find_conflicts([{"title": "x"}], 15)
    with pytest.raises(Exception):
        cm.find_conflicts([], "soon")


def test_find_conflicts_empty(cm):
    assert cm.find_conflicts([], 15) == {"overlaps": [], "tight": []}


# ── agenda ───────────────────────────────────────────────────────────────────

def _future(hours, minute=0):
    base = (datetime.now() + timedelta(hours=hours)).replace(minute=minute, second=0, microsecond=0)
    return base


def test_agenda_empty(cm, cfg, fake_hass):
    assert cm.agenda(fake_hass, 24) == {"events": [], "conflicts": [], "count": 0}
    assert cm.agenda(None) == {"events": [], "conflicts": [], "count": 0}


def test_agenda_lines_and_conflicts(cm, cfg, fake_hass, monkeypatch):
    s1 = _future(2, 0)
    s2 = s1 + timedelta(minutes=30)
    s3 = s1 + timedelta(minutes=95)
    events = [
        ev("Review", s2, s2 + timedelta(minutes=60)),
        ev("Dentist", s1, s1 + timedelta(minutes=60)),
        ev("Gym", s3, s3 + timedelta(minutes=30)),
        ev("Holiday", s1.replace(hour=0, minute=0), s1.replace(hour=0) + timedelta(days=1), all_day=True),
    ]
    monkeypatch.setattr(cm, "gather_events", lambda hass: list(events))
    r = cm.agenda(fake_hass, 24)
    starts = sorted(events, key=lambda e: e["start"])
    assert r["count"] == 4
    assert r["events"] == [
        f"{e['title']} — {'all day' if e['all_day'] else clock(e['start'])}" for e in starts]
    assert r["conflicts"] == [
        f"conflict: “Dentist” and “Review” overlap ({clock(s1)} / {clock(s2)})",
        "tight: only 5 min between “Review” and “Gym”",
    ]
    assert "error" not in r


def test_agenda_window(cm, cfg, fake_hass, monkeypatch):
    now = datetime.now()
    events = [
        ev("Past", now - timedelta(hours=3), now - timedelta(hours=2)),
        ev("Ongoing", now - timedelta(minutes=30), now + timedelta(minutes=30)),
        ev("Soon", now + timedelta(hours=1), now + timedelta(hours=2)),
        ev("Far", now + timedelta(hours=5), now + timedelta(hours=6)),
    ]
    monkeypatch.setattr(cm, "gather_events", lambda hass: list(events))
    r = cm.agenda(fake_hass, 3)
    assert [line.split(" — ")[0] for line in r["events"]] == ["Ongoing", "Soon"]
    assert r["count"] == 2


def test_agenda_horizon_minimum_one_hour(cm, cfg, fake_hass, monkeypatch):
    now = datetime.now()
    events = [ev("Half", now + timedelta(minutes=50), now + timedelta(minutes=55)),
              ev("Two", now + timedelta(minutes=120), now + timedelta(minutes=130))]
    monkeypatch.setattr(cm, "gather_events", lambda hass: list(events))
    assert cm.agenda(fake_hass, 0)["count"] == 1
    assert cm.agenda(fake_hass, -4)["count"] == 1


def test_agenda_uses_configured_gap(cm, cfg, fake_hass, monkeypatch):
    s = _future(2)
    a = ev("A", s, s + timedelta(minutes=30))
    b = ev("B", s + timedelta(minutes=50), s + timedelta(minutes=60))
    monkeypatch.setattr(cm, "gather_events", lambda hass: [a, b])
    assert cm.agenda(fake_hass)["conflicts"] == []                 # default 15
    cfg["calendar_tight_gap_min"] = 30
    assert cm.agenda(fake_hass)["conflicts"] == ["tight: only 20 min between “A” and “B”"]
    cfg["calendar_tight_gap_min"] = None
    assert cm.agenda(fake_hass)["conflicts"] == []


def test_agenda_tight_minutes_round_down(cm, cfg, fake_hass, monkeypatch):
    s = _future(2)
    a = ev("A", s, s + timedelta(minutes=30))
    b = ev("B", s + timedelta(minutes=37, seconds=50), s + timedelta(minutes=60))
    monkeypatch.setattr(cm, "gather_events", lambda hass: [a, b])
    assert cm.agenda(fake_hass)["conflicts"] == ["tight: only 7 min between “A” and “B”"]


def test_agenda_reads_real_calendars(cm, cfg, fake_hass):
    s = _future(3, 5)
    fake_hass.states.set("calendar.team", "off", message="Planning",
                         start_time=iso(s), end_time=iso(s + timedelta(hours=1)))
    r = cm.agenda(fake_hass, 24)
    assert r["events"] == [f"Planning — {clock(s)}"]
    assert r["count"] == 1


@pytest.mark.parametrize("dt,expected", [
    (datetime(2026, 1, 1, 0, 5), "12:05 AM"), (datetime(2026, 1, 1, 9, 5), "9:05 AM"),
    (datetime(2026, 1, 1, 12, 0), "12:00 PM"), (datetime(2026, 1, 1, 14, 30), "2:30 PM")])
def test_agenda_time_format(cm, cfg, fake_hass, monkeypatch, dt, expected):
    # Shift the event to the future while keeping its clock time.
    target = datetime.now().replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
    if target <= datetime.now():
        target += timedelta(days=1)
    monkeypatch.setattr(cm, "gather_events", lambda hass: [ev("X", target, target + timedelta(minutes=5))])
    assert cm.agenda(fake_hass, 48)["events"] == [f"X — {expected}"]


def test_agenda_error_shape(cm, cfg, fake_hass):
    cfg["calendar_tight_gap_min"] = "lots"
    r = cm.agenda(fake_hass, 24)
    assert r["events"] == [] and r["conflicts"] == [] and r["count"] == 0
    assert isinstance(r["error"], str) and r["error"]


def test_agenda_bad_horizon_error_shape(cm, cfg, fake_hass):
    r = cm.agenda(fake_hass, "tomorrow")
    assert r["count"] == 0 and "error" in r


def test_agenda_never_raises_on_gather_failure(cm, cfg, fake_hass, monkeypatch):
    def boom(hass):
        raise RuntimeError("calendar exploded")
    monkeypatch.setattr(cm, "gather_events", boom)
    assert cm.agenda(fake_hass) == {"events": [], "conflicts": [], "count": 0,
                                    "error": "calendar exploded"}


def test_agenda_config_failure_uses_default(cm, fake_hass, load, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no config")
    monkeypatch.setattr(load("nova_config"), "get", boom)
    s = _future(2)
    a = ev("A", s, s + timedelta(minutes=30))
    b = ev("B", s + timedelta(minutes=40), s + timedelta(minutes=60))
    monkeypatch.setattr(cm, "gather_events", lambda hass: [a, b])
    assert cm.agenda(fake_hass)["conflicts"] == ["tight: only 10 min between “A” and “B”"]
