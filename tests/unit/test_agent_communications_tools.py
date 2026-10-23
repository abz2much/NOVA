"""The agent tools for web research, the calendar and email: the arguments
each passes on, the JSON the model gets back, and the log lines."""
import json
import sys
import types

import pytest

from fakes import FakeHass

SAFE = "RuntimeError (details are in the Home Assistant log)"


@pytest.fixture
def comm(load):
    return load("agent_runtime.capabilities.communications")


def _stub(monkeypatch, name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return mod


@pytest.fixture
def logged(monkeypatch):
    lines = []
    _stub(monkeypatch, "websocket", nova_log=lambda *a, **k: lines.append((a, k)))
    return lines


# ── web research ─────────────────────────────────────────────────────────────

@pytest.fixture
def research(monkeypatch):
    box = types.SimpleNamespace(queries=[], result={}, error=None)

    async def run(hass, query):
        box.queries.append(query)
        if box.error:
            raise box.error
        return box.result

    _stub(monkeypatch, "web_research", research=run)
    return box


async def test_a_research_result_is_returned_and_logged_with_its_backend(
        comm, research, logged):
    research.result = {"query": "boiler pressure", "backend": "duckduckgo", "results": []}
    out = await comm._exec_web_research(FakeHass(), {"query": "boiler pressure"})
    assert json.loads(out) == research.result
    assert research.queries == ["boiler pressure"]
    assert logged == [(("AGENT", "web research 'boiler pressure' via duckduckgo"), {})]


async def test_a_research_error_in_the_result_is_logged_as_the_error(comm, research, logged):
    research.result = {"query": "q", "error": "no backend configured"}
    out = await comm._exec_web_research(FakeHass(), {"query": "q"})
    assert json.loads(out) == research.result
    assert logged == [(("AGENT", "web research 'q': no backend configured"), {})]


async def test_a_result_without_a_query_or_backend_still_logs(comm, research, logged):
    research.result = {"results": [1]}
    await comm._exec_web_research(FakeHass(), {})
    assert research.queries == [""]
    assert logged == [(("AGENT", "web research '' via None"), {})]


async def test_a_failing_log_does_not_lose_the_result(comm, research, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("log full")
    _stub(monkeypatch, "websocket", nova_log=boom)
    research.result = {"query": "q", "backend": "b"}
    assert json.loads(await comm._exec_web_research(FakeHass(), {"query": "q"})) == \
        research.result


async def test_a_failing_research_is_a_safe_error(comm, research, logged):
    research.error = RuntimeError("secret")
    out = await comm._exec_web_research(FakeHass(), {"query": "q"})
    assert json.loads(out) == {"error": SAFE} and logged == []


async def test_a_result_that_is_not_a_dict_is_a_safe_error(comm, research, logged):
    research.result = ["not", "a", "dict"]
    out = await comm._exec_web_research(FakeHass(), {"query": "q"})
    assert json.loads(out) == ["not", "a", "dict"]


# ── calendar ─────────────────────────────────────────────────────────────────

@pytest.fixture
def agenda(monkeypatch):
    box = types.SimpleNamespace(calls=[], result={"events": []}, error=None)

    def run(hass, horizon):
        box.calls.append(horizon)
        if box.error:
            raise box.error
        return box.result

    _stub(monkeypatch, "comms", agenda=run)
    return box


@pytest.mark.parametrize("args, horizon", [
    ({}, 24), ({"horizon_hours": 48}, 48), ({"horizon_hours": "6"}, 6),
    ({"horizon_hours": 0}, 24), ({"horizon_hours": None}, 24),
    ({"horizon_hours": 2.9}, 2)])
async def test_the_agenda_horizon_defaults_to_24_hours(comm, agenda, args, horizon):
    out = await comm._exec_calendar_agenda(FakeHass(), args)
    assert json.loads(out) == {"events": []}
    assert agenda.calls == [horizon]


async def test_a_bad_horizon_is_a_safe_error(comm, agenda):
    out = await comm._exec_calendar_agenda(FakeHass(), {"horizon_hours": "soon"})
    assert json.loads(out) == {"error": "ValueError (details are in the Home Assistant log)"}
    assert agenda.calls == []


async def test_a_failing_agenda_is_a_safe_error(comm, agenda):
    agenda.error = RuntimeError("secret")
    assert json.loads(await comm._exec_calendar_agenda(FakeHass(), {})) == {"error": SAFE}


# ── email ────────────────────────────────────────────────────────────────────

@pytest.fixture
def inbox(monkeypatch):
    box = types.SimpleNamespace(calls=[], result={"messages": []}, error=None)

    async def fetch_recent(hass, **kw):
        box.calls.append(kw)
        if box.error:
            raise box.error
        return box.result

    _stub(monkeypatch, "mail", fetch_recent=fetch_recent)
    return box


@pytest.mark.parametrize("args, expected", [
    ({}, {"limit": 5, "unread_only": False, "folder": None}),
    ({"limit": 10, "unread_only": True, "folder": "Work"},
     {"limit": 10, "unread_only": True, "folder": "Work"}),
    ({"limit": "3", "unread_only": 1, "folder": ""},
     {"limit": 3, "unread_only": True, "folder": None}),
    ({"limit": 0, "unread_only": "", "folder": None},
     {"limit": 5, "unread_only": False, "folder": None}),
    ({"limit": None, "folder": "INBOX"},
     {"limit": 5, "unread_only": False, "folder": "INBOX"})])
async def test_email_arguments_are_cleaned_before_the_fetch(comm, inbox, args, expected):
    out = await comm._exec_read_email(FakeHass(), args)
    assert json.loads(out) == {"messages": []}
    assert inbox.calls == [expected]


async def test_a_bad_email_limit_is_a_safe_error(comm, inbox):
    out = await comm._exec_read_email(FakeHass(), {"limit": "many"})
    assert json.loads(out) == {"error": "ValueError (details are in the Home Assistant log)"}
    assert inbox.calls == []


async def test_a_failing_fetch_is_a_safe_error(comm, inbox):
    inbox.error = RuntimeError("secret")
    assert json.loads(await comm._exec_read_email(FakeHass(), {})) == {"error": SAFE}


def test_the_agent_logger_keeps_the_name_it_always_had(comm):
    assert comm._LOGGER.name == "jc.agent"
