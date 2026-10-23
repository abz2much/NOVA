"""Home Assistant's intent loader awaits ``platform.async_setup_intents(hass)``.

A plain ``def`` there hands HA ``None`` to await, which raises
"TypeError: NoneType can't be awaited" and shows up for the user as
"Unexpected error during intent recognition" (GitHub issue #12). These checks
read the package source, so they hold without importing Home Assistant.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

INTENT_INIT = (Path(__file__).resolve().parents[2]
               / "custom_components" / "nova" / "intent" / "__init__.py")


@pytest.fixture(scope="module")
def intent_source() -> str:
    return INTENT_INIT.read_text(encoding="utf-8")


def _top_level_defs(tree: ast.Module, wanted: str) -> list[ast.AST]:
    """Every module-level function (sync or async) called ``wanted``."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return [node for node in tree.body if isinstance(node, kinds) and node.name == wanted]


def test_setup_hook_is_declared_with_async_def(intent_source):
    assert "async def async_setup_intents(" in intent_source
    stripped = (line.strip() for line in intent_source.splitlines())
    plain = [line for line in stripped if line.startswith("def async_setup_intents(")]
    assert plain == [], "a plain def would be awaited by HA and crash"


def test_exactly_one_coroutine_definition_takes_hass(intent_source):
    found = _top_level_defs(ast.parse(intent_source), "async_setup_intents")
    # One definition only: a later sync redefinition would silently win.
    assert len(found) == 1
    hook = found[0]
    assert isinstance(hook, ast.AsyncFunctionDef)
    assert [arg.arg for arg in hook.args.args] == ["hass"]


def test_hook_is_exported(intent_source):
    tree = ast.parse(intent_source)
    exported = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
    ]
    assert exported == [["async_setup_intents"]]


async def test_awaiting_the_hook_completes_quietly(intent_source, fake_hass):
    # Run the real definition in isolation: it must be awaitable and do nothing.
    namespace: dict = {}
    exec(compile(intent_source, str(INTENT_INIT), "exec"), namespace)
    hook = namespace["async_setup_intents"]
    assert inspect.iscoroutinefunction(hook)
    assert await hook(fake_hass) is None
    assert fake_hass.service_calls == []
