"""The web_research and calendar_agenda agent tools are wired up, and the
calendar agent (comms.py) finds clashes the way the agenda reports them.
Web research itself is tested in test_web_research.py and the finer points
of comms.py in test_comms.py; this file keeps the clash rules the tools
depend on pinned with fixed times."""
from datetime import datetime, timedelta

import pytest

DAY = datetime(2026, 7, 20)
COMMS_TOOLS = ("web_research", "calendar_agenda")


@pytest.fixture
def comms(load):
    return load("comms")


def slot(name, start, end, *, all_day=False):
    """An event as gather_events shapes it; start and end are hours (or
    fractions of one) after midnight on DAY."""
    return {"calendar": f"calendar.{name}", "title": name,
            "start": DAY + timedelta(hours=start), "end": DAY + timedelta(hours=end),
            "all_day": all_day, "active": True}


def titles(pairs):
    return [(first["title"], second["title"]) for first, second in pairs]


# ── the agent tools ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", COMMS_TOOLS)
def test_tool_is_offered_and_mapped(load, tool):
    agent = load("agent")
    offered = [t["function"]["name"] for t in agent.NOVA_TOOLS]
    assert offered.count(tool) == 1
    assert agent._TOOL_MAP[tool] is getattr(agent, f"_exec_{tool}")


# ── find_conflicts ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("events, overlaps, tight", [
    # An hour or more apart: nothing to say.
    ([slot("a", 9, 10), slot("b", 12, 13)], [], []),
    # A real double booking.
    ([slot("a", 9, 11), slot("b", 10, 12)], [("a", "b")], []),
    # Ten minutes to get from one to the next is under the 15 minute gap.
    ([slot("a", 9, 10), slot("b", 10 + 10 / 60, 11)], [], [("a", "b")]),
    # Back to back is a tight change-over, not a double booking.
    ([slot("a", 9, 10), slot("b", 10, 11)], [], [("a", "b")]),
    # A clear hour between them is fine.
    ([slot("a", 9, 10), slot("b", 11, 12)], [], []),
    # All-day events never clash with timed ones.
    ([slot("holiday", 0, 24, all_day=True), slot("meeting", 10, 11)], [], []),
    # Input order does not matter: pairs come out by start time.
    ([slot("late", 10, 12), slot("early", 9, 11)], [("early", "late")], []),
], ids=["spaced", "overlap", "tight", "back-to-back", "gap-ok", "all-day", "unsorted"])
def test_conflict_rules(comms, events, overlaps, tight):
    found = comms.find_conflicts(events, 15)
    assert titles(found["overlaps"]) == overlaps
    assert titles(found["tight"]) == tight


def test_a_gap_exactly_at_the_limit_is_not_tight(comms):
    found = comms.find_conflicts([slot("a", 9, 10), slot("b", 10.25, 11)], 15)
    assert found == {"overlaps": [], "tight": []}


def test_three_way_overlap_lists_every_pair(comms):
    found = comms.find_conflicts(
        [slot("a", 9, 12), slot("b", 10, 11), slot("c", 10.5, 13)], 15)
    assert titles(found["overlaps"]) == [("a", "b"), ("a", "c"), ("b", "c")]


def test_no_events_means_no_conflicts(comms):
    assert comms.find_conflicts([], 15) == {"overlaps": [], "tight": []}


# ── _parse ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("2026-07-20", datetime(2026, 7, 20, 0, 0)),
    ("2026-07-20T14:30:00", datetime(2026, 7, 20, 14, 30)),
    ("2026-07-20T14:30:00+00:00", datetime(2026, 7, 20, 14, 30)),
    ("2026-07-20T14:30:00Z", datetime(2026, 7, 20, 14, 30)),
    ("garbage", None),
    (None, None),
    ("", None),
    ("2026-13-40", None),
])
def test_parse(comms, raw, expected):
    # The unit test stand-in for Home Assistant's clock is UTC, so a UTC
    # stamp keeps its wall-clock time.
    got = comms._parse(raw)
    assert got == expected
    assert got is None or got.tzinfo is None


def test_parse_moves_an_offset_into_home_assistant_time(comms, monkeypatch):
    import sys
    from datetime import timezone
    two_ahead = timezone(timedelta(hours=2))
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "as_local",
                        lambda value: value.astimezone(two_ahead))
    assert comms._parse("2026-07-20T14:30:00Z") == datetime(2026, 7, 20, 16, 30)
    # No offset written: already local, left alone.
    assert comms._parse("2026-07-20T14:30:00") == datetime(2026, 7, 20, 14, 30)


# ── agenda ──────────────────────────────────────────────────────────────────

def test_agenda_never_raises_without_calendars(comms, fake_hass):
    out = comms.agenda(fake_hass, 24)
    assert out == {"events": [], "conflicts": [], "count": 0}


def test_agenda_with_no_hass_is_empty_not_an_error(comms):
    assert comms.agenda(None, 24) == {"events": [], "conflicts": [], "count": 0}


def test_agenda_reads_calendars_and_speaks_the_clash(comms, fake_hass, monkeypatch):
    monkeypatch.setattr(comms, "_now", lambda: DAY + timedelta(hours=8))
    monkeypatch.setattr(comms, "_tight_gap", lambda: 15)
    fake_hass.states.set("calendar.work", "off", message="Stand-up",
                         start_time="2026-07-20 09:00:00", end_time="2026-07-20 09:30:00")
    fake_hass.states.set("calendar.family", "off", message="School run",
                         start_time="2026-07-20 09:15:00", end_time="2026-07-20 09:45:00")
    fake_hass.states.set("calendar.gym", "off", message="Swim",
                         start_time="2026-07-20 09:50:00", end_time="2026-07-20 10:30:00")
    out = comms.agenda(fake_hass, 6)
    assert out["count"] == 3
    assert out["events"] == ["Stand-up — 9:00 AM", "School run — 9:15 AM", "Swim — 9:50 AM"]
    assert out["conflicts"] == [
        "conflict: “Stand-up” and “School run” overlap (9:00 AM / 9:15 AM)",
        "tight: only 5 min between “School run” and “Swim”",
    ]
