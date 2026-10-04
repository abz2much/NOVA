"""Safe error text for the model and the panel (v8.7.4).

A raw str(exc) carried file paths, host names and library detail to the
cloud model and to the panel, including the panel commands any signed-in
user may call. Errors now leave the server as the exception type plus a
pointer to the log; only text that is safe by construction (ProviderError)
or that Home Assistant itself returns to the model (HomeAssistantError, for
tool calls) is kept. Error codes are unchanged (pinned by the contracts).
"""
import ast
import json
import logging
import pathlib
import sys
import types

import pytest

from fakes import FakeHass, FakeUserInput
from ws_sources import ws_tree

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


@pytest.fixture
def se(load):
    return load("safe_errors")


@pytest.fixture
def ha_error(monkeypatch):
    """A stand-in homeassistant.exceptions (the unit stubs have none)."""
    mod = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ServiceValidationError(HomeAssistantError):
        pass

    mod.HomeAssistantError = HomeAssistantError
    mod.ServiceValidationError = ServiceValidationError
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", mod)
    return mod


def test_generic_message_keeps_type_and_drops_detail(se):
    exc = FileNotFoundError(2, "No such file", "/config/nova/knowledge.db")
    msg = se.safe_error_message(exc)
    assert msg.startswith("FileNotFoundError")
    assert "/config" not in msg and "knowledge" not in msg


def test_provider_error_text_is_kept(se, load):
    errors = load("providers.errors")
    exc = errors.ProviderError(errors.ProviderErrorKind.RATE_LIMITED, "groq", status=429)
    msg = se.safe_error_message(exc)
    assert "rate limit" in msg and "HTTP 429" in msg


def test_ha_error_text_only_with_keep_ha_text(se, ha_error):
    exc = ha_error.ServiceValidationError("Entity light.x not found")
    assert "light.x" in se.safe_error_message(exc, keep_ha_text=True)
    assert "light.x" not in se.safe_error_message(exc)


def test_other_errors_never_keep_text_even_with_keep_ha_text(se, ha_error):
    msg = se.safe_error_message(RuntimeError("http://user:pw@10.0.0.2"), keep_ha_text=True)
    assert "10.0.0.2" not in msg and "pw" not in msg


def test_log_true_logs_the_full_exception(se, caplog):
    try:
        raise ValueError("secret detail /config/x")
    except ValueError as exc:
        with caplog.at_level(logging.WARNING):
            se.safe_error_message(exc, where="thing", log=True)
    assert "secret detail /config/x" in caplog.text      # the server keeps it
    assert "Traceback" in caplog.text


def test_nova_validation_text_is_kept_and_not_logged(se, caplog):
    exc = se.NovaValidationError("Prompt size must be between 0 and 50")
    assert isinstance(exc, ValueError)
    with caplog.at_level(logging.WARNING):
        assert se.safe_error_message(exc, log=True) == "Prompt size must be between 0 and 50"
    assert caplog.text == ""


def test_plain_value_error_is_not_treated_as_nova_validation(se):
    msg = se.safe_error_message(ValueError("Invalid URL 'http://u:p@10.0.0.2'"))
    assert "10.0.0.2" not in msg and msg.startswith("ValueError")


def test_ai_settings_validation_raises_are_nova_validation_errors():
    """Nova's own messages in the AI settings handlers keep reaching the
    settings screen: every raise there is a NovaValidationError."""
    tree = ws_tree()
    names = {"_prepare_ai_config_updates", "ws_test_provider_endpoint"}
    raised = [
        node.exc.func.id
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in names
        for node in ast.walk(fn)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
    ]
    assert raised and set(raised) == {"NovaValidationError"}


def test_long_kept_text_is_bounded(se, ha_error):
    msg = se.safe_error_message(ha_error.HomeAssistantError("x" * 1000), keep_ha_text=True)
    assert len(msg) <= 240


# ── The model: tool failures through the dispatcher ──────────────────────────

async def test_nova_tool_failure_returns_safe_text(load, monkeypatch):
    agent = load("agent")

    async def boom(*a, **k):
        raise OSError("/config/nova/patterns.db is locked")

    monkeypatch.setitem(agent._TOOL_MAP, "get_home_summary", boom)
    out = json.loads(await agent._execute_tool(FakeHass(), "get_home_summary", {}, None,
                                               FakeUserInput()))
    assert out["error"].startswith("OSError")
    assert "/config" not in out["error"]


async def test_nova_tool_ha_error_text_reaches_the_model(load, monkeypatch, ha_error):
    agent = load("agent")

    async def invalid(*a, **k):
        raise ha_error.ServiceValidationError("Entity light.x not found")

    monkeypatch.setitem(agent._TOOL_MAP, "get_home_summary", invalid)
    out = json.loads(await agent._execute_tool(FakeHass(), "get_home_summary", {}, None,
                                               FakeUserInput()))
    assert "Entity light.x not found" in out["error"]


class _FailingAssistApi:
    tools: list = []

    async def async_call_tool(self, tool_input):
        raise RuntimeError("aiohttp ClientConnectorError http://10.0.0.2:8123")


async def test_assist_tool_failure_returns_safe_text(load, monkeypatch):
    agent = load("agent")
    ap = load("assist_policy")
    decision = types.SimpleNamespace(
        allowed=True, classification=types.SimpleNamespace(mutating=True))

    async def authorize(*a, **k):
        return decision

    async def record(*a, **k):
        return None

    monkeypatch.setattr(ap, "async_authorize", authorize)
    monkeypatch.setattr(ap, "async_record_execution", record)
    out = json.loads(await agent._execute_tool(FakeHass(), "HassTurnOn", {},
                                               _FailingAssistApi(), FakeUserInput()))
    assert out["error"].startswith("HassTurnOn failed: RuntimeError")
    assert "10.0.0.2" not in out["error"]


# ── The panel: source guards on websocket.py ─────────────────────────────────



def _handlers_returning_raw_exc():
    tree = ws_tree()
    found = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "str" and node.args
                    and isinstance(node.args[0], ast.Name) and node.args[0].id == "exc"):
                found.add(fn.name)
    return found


def test_websocket_returns_no_raw_exception_text():
    assert _handlers_returning_raw_exc() == set()


def test_dispatcher_returns_no_raw_exception_text():
    src = (COMP / "agent_runtime" / "dispatcher.py").read_text(encoding="utf-8")
    assert "str(exc)" not in src and "{exc}" not in src
