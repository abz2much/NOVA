"""Working across Home Assistant's LLM API changes (v7.21.1).

Home Assistant 2026.8 moved the request context out of ToolInput and took
user_prompt off LLMContext. Nova builds both through _ha_kwargs, which keeps
only the keyword arguments a given constructor accepts, so the same code runs
on older and newer Home Assistant releases.
"""
from __future__ import annotations

import pytest

# Everything Nova may offer when it builds a ToolInput or an LLMContext.
OFFERED = {"tool_name": "x", "tool_args": {}, "platform": "d", "context": "c",
           "user_prompt": "p", "language": "en", "assistant": "a",
           "device_id": "dev"}


class ToolInput2026_8:
    """ToolInput as of 2026.8: no request context."""

    def __init__(self, tool_name, tool_args, id="", external=False):
        pass


class ToolInputBefore2026_8:
    """ToolInput before 2026.8, which still carried the context."""

    def __init__(self, tool_name, tool_args, platform=None, context=None,
                 user_prompt="", language="en", assistant="", device_id=None):
        pass


class LLMContext2026_8:
    """LLMContext as of 2026.8: user_prompt removed."""

    def __init__(self, platform, context, language, assistant, device_id):
        pass


@pytest.fixture
def agent_mod(load):
    return load("agent")


def _offer(agent_mod, cls, *names):
    return agent_mod._ha_kwargs(cls, **{n: OFFERED[n] for n in names})


@pytest.mark.parametrize("cls, offered, kept", [
    pytest.param(ToolInput2026_8,
                 ("tool_name", "tool_args", "platform", "context", "user_prompt"),
                 {"tool_name", "tool_args"}, id="new-toolinput-drops-context"),
    pytest.param(ToolInputBefore2026_8, tuple(OFFERED), set(OFFERED),
                 id="old-toolinput-keeps-everything"),
    pytest.param(LLMContext2026_8,
                 ("platform", "context", "user_prompt", "language", "assistant", "device_id"),
                 {"platform", "context", "language", "assistant", "device_id"},
                 id="new-llmcontext-drops-user-prompt"),
])
def test_only_accepted_arguments_survive(agent_mod, cls, offered, kept):
    result = _offer(agent_mod, cls, *offered)
    assert set(result) == kept
    # Spelled out as well: the context fields survive on the old class, and
    # user_prompt never reaches the new context.
    if cls is ToolInputBefore2026_8:
        assert {"platform", "context", "user_prompt", "device_id"} <= set(result)
    if cls is LLMContext2026_8:
        assert "user_prompt" not in result and "platform" in result


def test_values_pass_through_unchanged(agent_mod):
    result = _offer(agent_mod, ToolInputBefore2026_8, *OFFERED)
    assert result == OFFERED
    # Same objects, not copies: tool_args is handed to Home Assistant as is.
    assert result["tool_args"] is OFFERED["tool_args"]


def test_filtered_arguments_build_the_class(agent_mod):
    # The point of the filter: calling the constructor with the result works
    # on a class that would reject the full set.
    fields = _offer(agent_mod, ToolInput2026_8, *OFFERED)
    ToolInput2026_8(**fields)
    with pytest.raises(TypeError):
        ToolInput2026_8(**OFFERED)


def test_unreadable_signature_passes_everything(agent_mod):
    # When inspect cannot read a signature the arguments go through as they
    # are rather than being silently dropped.
    class NoSignature:
        __signature__ = "not a signature"
    assert _offer(agent_mod, NoSignature, "tool_name", "language") == {
        "tool_name": "x", "language": "en"}


def test_no_arguments_gives_no_arguments(agent_mod):
    assert agent_mod._ha_kwargs(ToolInput2026_8) == {}


def test_dispatcher_builds_tool_input_without_a_turn(load):
    # With no conversation turn the dispatcher still builds a ToolInput the
    # installed Home Assistant (the stand-in takes only name and args) accepts.
    dispatcher = load("agent_runtime.dispatcher")
    made = dispatcher._assist_tool_input("HassTurnOn", {"name": "light.kitchen"}, None)
    assert made.tool_name == "HassTurnOn"
    assert made.tool_args == {"name": "light.kitchen"}
