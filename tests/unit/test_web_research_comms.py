"""Tests for the calendar agent's conflict analysis, and that both the web
research and calendar tools are registered. Web research itself is covered
in test_web_research.py."""
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def comms(load):
    return load("comms")


def test_new_agent_tools_registered(load):
    agent = load("agent")
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert {"web_research", "calendar_agenda"} <= names
    assert "web_research" in agent._TOOL_MAP
    assert "calendar_agenda" in agent._TOOL_MAP


# ── comms conflict detection ────────────────────────────────────────────────

def _ev(title, start_h, end_h, all_day=False):
    base = datetime(2026, 7, 20, 0, 0)
    return {"calendar": f"calendar.{title}", "title": title,
            "start": base + timedelta(hours=start_h),
            "end": base + timedelta(hours=end_h),
            "all_day": all_day, "active": True}


def test_no_conflicts_when_spaced(comms):
    evts = [_ev("a", 9, 10), _ev("b", 12, 13)]
    r = comms.find_conflicts(evts, 15)
    assert r["overlaps"] == [] and r["tight"] == []


def test_overlap_detected(comms):
    evts = [_ev("a", 9, 11), _ev("b", 10, 12)]
    r = comms.find_conflicts(evts, 15)
    assert len(r["overlaps"]) == 1
    a, b = r["overlaps"][0]
    assert {a["title"], b["title"]} == {"a", "b"}


def test_tight_transition_flagged(comms):
    evts = [_ev("a", 9, 10), {"calendar": "calendar.b", "title": "b",
            "start": datetime(2026, 7, 20, 10, 10), "end": datetime(2026, 7, 20, 11, 0),
            "all_day": False, "active": True}]
    r = comms.find_conflicts(evts, 15)
    assert len(r["tight"]) == 1 and r["overlaps"] == []


def test_tight_not_flagged_when_gap_sufficient(comms):
    evts = [_ev("a", 9, 10), _ev("b", 11, 12)]   # 60-min gap
    r = comms.find_conflicts(evts, 15)
    assert r["tight"] == []


def test_all_day_events_dont_conflict(comms):
    evts = [_ev("holiday", 0, 24, all_day=True), _ev("meeting", 10, 11)]
    r = comms.find_conflicts(evts, 15)
    assert r["overlaps"] == [] and r["tight"] == []


def test_parse_handles_date_and_datetime(comms):
    assert comms._parse("2026-07-20") == datetime(2026, 7, 20, 0, 0)
    assert comms._parse("2026-07-20T14:30:00") == datetime(2026, 7, 20, 14, 30)
    assert comms._parse("2026-07-20T14:30:00+00:00") == datetime(2026, 7, 20, 14, 30)
    assert comms._parse("2026-07-20T14:30:00Z") == datetime(2026, 7, 20, 14, 30)
    assert comms._parse("garbage") is None
    assert comms._parse(None) is None


def test_agenda_never_raises_without_calendars(comms, fake_hass):
    out = comms.agenda(fake_hass, 24)
    assert out["count"] == 0 and out["events"] == [] and out["conflicts"] == []
