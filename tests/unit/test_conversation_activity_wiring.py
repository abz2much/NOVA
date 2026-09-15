"""_llm_text/_llm_with_tools now route through llm_provider.chat_with_
activity() (Phase 5) instead of a bare self._client.chat() wrapped in
hass.async_add_executor_job. Source-level guard, same reasoning as
test_conversation_dispatch.py's: NovaAgent extends HA's own
conversation.ConversationEntity, so exercising it needs a live HA
conversation stack — chat_with_activity's own behavior (success/failure/
recording) is covered directly in test_llm_provider_usage.py; this only
proves the main conversation path was actually migrated to it, not left on
the old direct-call pattern.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "conversation.py"


def _function_source(name: str) -> str:
    tree = ast.parse(SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(SRC.read_text(), node)
    raise AssertionError(f"{name} not found in {SRC}")


def test_llm_text_routes_through_chat_with_activity():
    src = _function_source("_llm_text")
    assert "llm_provider.chat_with_activity" in src
    assert 'role="llm"' in src
    assert 'data_category="text"' in src
    # not the old direct-call pattern
    assert "self._client.chat(" not in src


def test_llm_with_tools_routes_through_chat_with_activity():
    src = _function_source("_llm_with_tools")
    assert "llm_provider.chat_with_activity" in src
    assert 'role="llm"' in src
    assert 'data_category="text"' in src
    assert "self._client.chat(" not in src


def test_agentic_loop_calls_the_migrated_methods_directly():
    """The three call sites inside _agentic_loop must await the (now async)
    helper methods directly, not re-wrap them in hass.async_add_executor_job
    — chat_with_activity already does that internally, so a leftover outer
    executor-job wrap would double-wrap a coroutine incorrectly."""
    src = _function_source("_agentic_loop")
    assert "await self._llm_text(" in src
    assert "await self._llm_with_tools(" in src
    assert "async_add_executor_job(self._llm_text" not in src
    assert "async_add_executor_job(self._llm_with_tools" not in src
