"""Source level guards on how the conversation entity meets Home Assistant.

ConversationEntity.async_process is @final in Home Assistant. It prepares the
chat session and chat log, then calls _async_handle_message. Nova therefore
implements only _async_handle_message and never overrides async_process. A
shim that did (v7.48.1) skipped that preparation on current Home Assistant and
turned voice turns into an opaque "Unexpected error during intent
recognition". The checks read the source because running the entity needs a
live Home Assistant conversation stack.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "conversation.py"


@pytest.fixture(scope="module")
def source():
    return SRC.read_text()


def _nova_agent(source):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == "NovaAgent":
            return node
    raise AssertionError("NovaAgent class not found")


def _method_names(cls):
    return {item.name for item in cls.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_async_process_is_left_to_home_assistant(source):
    # It is @final upstream, so the entity must not define it.
    assert "async def async_process(" not in source
    assert "async_process" not in _method_names(_nova_agent(source))


def test_a_crash_in_the_handler_is_caught_and_traced(source):
    # The method Home Assistant calls hands over to an implementation inside a
    # try block, so a crash leaves a trace instead of Home Assistant's opaque
    # error.
    assert "async def _async_handle_message(" in source
    assert "async def _handle_message_impl(" in source
    assert "conversation handler crashed" in source


def test_the_handler_belongs_to_the_agent_class_not_to_the_module():
    """The handler must be a method of NovaAgent, the class that gets
    registered. A stray statement at column 0 once closed the class early and
    left the whole handler as dead code, so Home Assistant fell back to its
    own _async_handle_message and raised NotImplementedError. The words
    appearing in the file would not catch that, so class membership is
    checked."""
    methods = _method_names(_nova_agent(SRC.read_text()))
    assert "_async_handle_message" in methods, \
        "_async_handle_message is not a method of NovaAgent (orphaned?)"
    assert "_handle_message_impl" in methods


def test_the_context_names_the_temperature_unit_the_home_uses(source):
    """Free-form model output has to follow Home Assistant's unit system and
    not drift to Fahrenheit. The live context reads the configured
    temperature unit and tells the model to give temperatures in it, so a
    metric household hears Celsius even when Nova is not quoting a sensor."""
    assert "config.units.temperature_unit" in source, \
        "conversation context must read HA's configured temperature unit"
    assert "temperatures in" in source, \
        "context must instruct the model which temperature unit to use"
