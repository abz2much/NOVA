"""Acceptance tests for activity_history.py, written from its behaviour spec."""
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeHass

NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


class Recorder:
    """Fake recorder + logbook components."""

    def __init__(self):
        self.history = {}
        self.history_exc = None
        self.history_calls = []
        self.executor_calls = 0
        self.logbook_rows = []
        self.executor_exc = None

    def get_significant_states(self, hass, start, end, ids, **kw):
        self.history_calls.append((hass, start, end, list(ids), kw))
        if self.history_exc:
            raise self.history_exc
        return self.history


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()

    class Instance:
        async def async_add_executor_job(self, fn, *args):
            r.executor_calls += 1
            if r.executor_exc:
                raise r.executor_exc
            return fn(*args)

    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.get_instance = lambda hass: Instance()
    recorder.history = types.SimpleNamespace(get_significant_states=r.get_significant_states)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)

    lb = types.ModuleType("homeassistant.components.logbook")
    r.logbook_calls = []

    def async_get_events(hass, start, end, entity_ids=None):
        r.logbook_calls.append(("async_get_events", start, end, entity_ids))
        return list(r.logbook_rows)
    lb.async_get_events = async_get_events
    monkeypatch.setitem(sys.modules, "homeassistant.components.logbook", lb)
    r.lb = lb
    monkeypatch.setattr(sys.modules["homeassistant.util.dt"], "utcnow", lambda: NOW)
    return r


@pytest.fixture
def ah(load, rec):
    return load("activity_history")


@pytest.fixture
def hass():
    h = FakeHass()
    h.states.set("binary_sensor.back_door", "off", friendly_name="Back Door")
    h.states.set("light.hall", "on", friendly_name="Hall Light")
    h.states.set("light.landing", "off", friendly_name="Landing Lamp")
    h.states.set("sensor.door_count", "3")
    return h


@pytest.fixture
def registries(monkeypatch):
    data = {"areas": [], "by_area": {}}
    ar = sys.modules["homeassistant.helpers.area_registry"]
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_list_areas=lambda: [types.SimpleNamespace(id=i, name=n) for i, n in data["areas"]]))
    monkeypatch.setattr(er, "async_get", lambda hass: "ENTITY_REGISTRY")

    def entries_for_area(reg, area_id):
        assert reg == "ENTITY_REGISTRY"
        return [types.SimpleNamespace(entity_id=e) for e in data["by_area"].get(area_id, [])]
    monkeypatch.setattr(er, "async_entries_for_area", entries_for_area, raising=False)
    return data


class Row:
    def __init__(self, state, changed=None, updated=None):
        self.state = state
        self.last_changed = changed
        self.last_updated = updated


def at(h, m=0):
    return datetime(2026, 5, 4, h, m, tzinfo=timezone.utc)


# ── _resolve_entities ────────────────────────────────────────────────────────

def test_resolve_exact_id(ah, hass):
    assert ah._resolve_entities(hass, "light.hall", "Kitchen") == ["light.hall"]


def test_resolve_exact_id_is_case_sensitive_then_substring(ah, hass):
    assert ah._resolve_entities(hass, "LIGHT.HALL", None) == ["light.hall"]


def test_resolve_by_friendly_name_and_id_substring(ah, hass):
    assert ah._resolve_entities(hass, "Door", None) == ["binary_sensor.back_door", "sensor.door_count"]
    assert ah._resolve_entities(hass, "lamp", None) == ["light.landing"]


def test_resolve_no_match(ah, hass):
    assert ah._resolve_entities(hass, "submarine", None) == []


def test_resolve_cap_twenty(ah):
    h = FakeHass()
    for i in range(30):
        h.states.set(f"switch.plug_{i}", "off", friendly_name=f"Plug {i}")
    assert len(ah._resolve_entities(h, "plug", None)) == 20


def test_resolve_neither(ah, hass):
    assert ah._resolve_entities(hass, None, None) == []
    assert ah._resolve_entities(hass, "", "") == []


def test_resolve_area(ah, hass, registries):
    registries["areas"] = [("a0", ""), ("a1", "Upstairs Hall"), ("a2", "Hall")]
    registries["by_area"] = {"a1": ["light.landing"], "a2": ["light.hall"]}
    assert ah._resolve_entities(hass, None, "HALL") == ["light.landing"]   # first substring match
    assert ah._resolve_entities(hass, None, "garden") == []


def test_resolve_area_cap(ah, hass, registries):
    registries["areas"] = [("a1", "Garage")]
    registries["by_area"] = {"a1": [f"switch.s{i}" for i in range(25)]}
    assert ah._resolve_entities(hass, None, "garage") == [f"switch.s{i}" for i in range(20)]


def test_resolve_area_registry_error(ah, hass, monkeypatch):
    def boom(h):
        raise RuntimeError("x")
    monkeypatch.setattr(sys.modules["homeassistant.helpers.area_registry"], "async_get", boom)
    assert ah._resolve_entities(hass, None, "hall") == []


def test_resolve_never_raises(ah):
    broken = types.SimpleNamespace(states=None)
    assert ah._resolve_entities(broken, "light.x", None) == []


# ── entity_history ───────────────────────────────────────────────────────────

async def test_history_timeline_counts_and_order(ah, rec, hass):
    rec.history = {
        "binary_sensor.back_door": [Row("on", at(8)), Row("off", at(9)),
                                    {"state": "on", "last_changed": at(11)}],
        "light.hall": [Row("on", None, at(10)), Row(None, at(10, 30))],
    }
    res = await ah.entity_history(hass, entity="binary_sensor.back_door", hours=6)
    assert res["entities"] == ["binary_sensor.back_door"]
    assert res["hours"] == 6.0
    assert res["counts"] == {"binary_sensor.back_door": 3, "light.hall": 1}
    assert [(r["entity_id"], r["state"], r["when"]) for r in res["timeline"]] == [
        ("binary_sensor.back_door", "on", at(11).isoformat()),
        ("light.hall", "on", at(10).isoformat()),
        ("binary_sensor.back_door", "off", at(9).isoformat()),
        ("binary_sensor.back_door", "on", at(8).isoformat()),
    ]
    assert "note" not in res


async def test_history_query_arguments(ah, rec, hass):
    rec.history = {"light.hall": []}
    await ah.entity_history(hass, entity="light.hall", hours=2)
    (h, start, end, ids, kw), = rec.history_calls
    assert h is hass and ids == ["light.hall"]
    assert end == NOW and start == NOW - timedelta(hours=2)
    assert kw == {"minimal_response": True, "no_attributes": True}
    assert rec.executor_calls == 1


async def test_history_dict_rows_and_string_times(ah, rec, hass):
    rec.history = {"light.hall": [{"state": "off", "last_updated": "2026-05-04T07:00:00"},
                                  {"state": "on"}, {"last_changed": at(3)}]}
    res = await ah.entity_history(hass, entity="light.hall")
    assert res["counts"] == {"light.hall": 2}
    # sorted by the "when" text, largest first: "None" sorts above digits
    assert [r["when"] for r in res["timeline"]] == ["None", "2026-05-04T07:00:00"]


async def test_history_zero_count_entity(ah, rec, hass):
    rec.history = {"light.hall": [Row("on", at(1))], "light.landing": []}
    res = await ah.entity_history(hass, entity="l")
    assert res["counts"]["light.landing"] == 0


async def test_history_cap_200_rows(ah, rec, hass):
    rows = [Row(str(i), datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(minutes=i)) for i in range(250)]
    rec.history = {"light.hall": rows}
    res = await ah.entity_history(hass, entity="light.hall", hours=720)
    assert len(res["timeline"]) == 200
    assert res["counts"]["light.hall"] == 250
    assert res["timeline"][0]["state"] == "249"


async def test_history_empty_window(ah, rec, hass):
    rec.history = {}
    res = await ah.entity_history(hass, entity="light.hall", hours=3)
    assert res == {"entities": ["light.hall"], "hours": 3.0, "timeline": [], "counts": {},
                   "note": "no recorded changes in that window"}


@pytest.mark.parametrize("given,expected", [(0, 0.1), (-5, 0.1), (100000, 720.0),
                                            ("12", 12.0), ("soon", 24.0), (None, 24.0)])
async def test_history_hours_clamp(ah, rec, hass, given, expected):
    res = await ah.entity_history(hass, entity="light.hall", hours=given)
    assert res["hours"] == pytest.approx(expected)


async def test_history_default_hours(ah, rec, hass):
    res = await ah.entity_history(hass, entity="light.hall")
    assert res["hours"] == 24.0


async def test_history_no_match_errors(ah, rec, hass, registries):
    assert await ah.entity_history(hass, entity="submarine") == {
        "error": "no matching entities for entity=submarine"}
    assert await ah.entity_history(hass, area="Attic") == {
        "error": "no matching entities for area=Attic"}
    assert await ah.entity_history(hass) == {"error": "no matching entities for area=None"}


async def test_history_by_area(ah, rec, hass, registries):
    registries["areas"] = [("a1", "Hallway")]
    registries["by_area"] = {"a1": ["light.hall"]}
    rec.history = {"light.hall": [Row("on", at(5))]}
    res = await ah.entity_history(hass, area="hallway")
    assert res["entities"] == ["light.hall"] and res["counts"] == {"light.hall": 1}


async def test_history_fetch_failure(ah, rec, hass):
    rec.history_exc = RuntimeError("db locked")
    assert await ah.entity_history(hass, entity="light.hall") == {
        "error": "could not read history for that period"}


async def test_history_executor_failure(ah, rec, hass):
    rec.executor_exc = RuntimeError("busy")
    assert await ah.entity_history(hass, entity="light.hall") == {
        "error": "could not read history for that period"}


async def test_history_recorder_missing(ah, rec, hass, monkeypatch):
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", None)
    assert await ah.entity_history(hass, entity="submarine") == {
        "error": "history recorder is not available"}


# ── logbook ──────────────────────────────────────────────────────────────────

async def test_logbook_entries(ah, rec, hass):
    rec.logbook_rows = [
        {"when": "2026-05-04T09:00:00Z", "name": "Back Door", "message": "opened",
         "entity_id": "binary_sensor.back_door"},
        {"timestamp": 1714813200, "state": "on"},
        "not a dict",
        {},
    ]
    res = await ah.logbook(hass, hours=5)
    assert res == {"hours": 5.0, "entries": [
        {"when": "2026-05-04T09:00:00Z", "name": "Back Door", "message": "opened",
         "entity_id": "binary_sensor.back_door"},
        {"when": "1714813200", "name": "", "message": "on", "entity_id": None},
        {"when": "", "name": "", "message": "", "entity_id": None},
    ]}
    assert rec.logbook_calls == [("async_get_events", NOW - timedelta(hours=5), NOW, None)]
    assert rec.executor_calls == 1


async def test_logbook_scoped_to_resolved_entity(ah, rec, hass):
    rec.logbook_rows = [{"name": "x"}]
    await ah.logbook(hass, entity="back door")
    assert rec.logbook_calls[-1][3] == ["binary_sensor.back_door"]


async def test_logbook_unresolved_entity_used_raw(ah, rec, hass):
    rec.logbook_rows = [{"name": "x"}]
    await ah.logbook(hass, entity="sensor.mystery")
    assert rec.logbook_calls[-1][3] == ["sensor.mystery"]


async def test_logbook_cap_100(ah, rec, hass):
    rec.logbook_rows = [{"name": str(i)} for i in range(150)]
    res = await ah.logbook(hass)
    assert len(res["entries"]) == 100 and res["entries"][-1]["name"] == "99"


async def test_logbook_cap_applies_before_filtering_quirk(ah, rec, hass):
    rec.logbook_rows = ["x"] * 100 + [{"name": "late"}]
    assert await ah.logbook(hass) == {"hours": 24.0, "entries": []}


async def test_logbook_empty(ah, rec, hass):
    assert await ah.logbook(hass, hours=2) == {
        "hours": 2.0, "entries": [],
        "note": "no logbook entries for that window (or logbook unavailable on this HA build)"}


@pytest.mark.parametrize("given,expected", [(0, 0.1), (999999, 336.0), ("x", 24.0)])
async def test_logbook_hours_clamp(ah, rec, hass, given, expected):
    res = await ah.logbook(hass, hours=given)
    assert res["hours"] == pytest.approx(expected)


async def test_logbook_falls_back_through_functions(ah, rec, hass, monkeypatch):
    calls = []

    def broken(*a):
        calls.append("async_get_events")
        raise RuntimeError("nope")

    def get_events(hass_, start, end, ids=None):
        calls.append(("get_events", ids))
        return iter([{"name": "ok"}])
    monkeypatch.setattr(rec.lb, "async_get_events", broken)
    monkeypatch.setattr(rec.lb, "get_events", get_events, raising=False)
    res = await ah.logbook(hass, entity="light.hall")
    assert res["entries"][0]["name"] == "ok"
    assert calls == ["async_get_events", ("get_events", ["light.hall"])]


async def test_logbook_private_function_last(ah, rec, hass, monkeypatch):
    monkeypatch.delattr(rec.lb, "async_get_events")
    monkeypatch.setattr(rec.lb, "_get_events", lambda h, s, e: [{"name": "old"}], raising=False)
    res = await ah.logbook(hass)
    assert res["entries"][0]["name"] == "old"


async def test_logbook_typeerror_retries_unscoped_quirk(ah, rec, hass, monkeypatch):
    calls = []

    def only_three(hass_, start, end):
        calls.append("unscoped")
        return [{"name": "whole house"}]
    monkeypatch.setattr(rec.lb, "async_get_events", only_three)
    res = await ah.logbook(hass, entity="light.hall")
    assert res["entries"][0]["name"] == "whole house"
    assert calls == ["unscoped"]


async def test_logbook_all_functions_fail(ah, rec, hass, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(rec.lb, "async_get_events", boom)
    res = await ah.logbook(hass)
    assert res["entries"] == [] and "note" in res


async def test_logbook_executor_failure(ah, rec, hass):
    rec.executor_exc = RuntimeError("busy")
    res = await ah.logbook(hass)
    assert res["entries"] == [] and "note" in res


async def test_logbook_component_missing(ah, rec, hass, monkeypatch):
    monkeypatch.delitem(sys.modules, "homeassistant.components.logbook")
    monkeypatch.delattr(sys.modules["homeassistant.components"], "logbook", raising=False)
    assert await ah.logbook(hass) == {"error": "logbook is not available"}


async def test_logbook_recorder_missing(ah, rec, hass, monkeypatch):
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", None)
    assert await ah.logbook(hass) == {"error": "logbook is not available"}
