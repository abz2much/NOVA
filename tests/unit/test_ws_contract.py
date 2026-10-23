"""The panel's `nova/*` WebSocket commands against the backend that serves them.

The dashboard only works while both ends agree on the command names. A
handler renamed in a refactor, or one that never makes it into the
registration list, fails quietly in the browser. These checks read the source
on both sides (no Home Assistant or voluptuous import is needed) and compare:

  * what nova-panel.js sends,
  * which handlers declare a command type through @websocket_command,
  * which handlers async_register() actually hands to Home Assistant.
"""
from __future__ import annotations

import ast
import collections
import functools
import pathlib
import re

import pytest

from ws_sources import ws_text, ws_tree

PANEL_SOURCE = (pathlib.Path(__file__).resolve().parents[2]
                / "custom_components" / "nova" / "frontend" / "nova-panel.js")

# Parsing every websocket module takes a while; the source does not change
# during a run, so the tree is built once.
_parsed_tree = functools.lru_cache(maxsize=1)(ws_tree)

# A `type: "nova/…"` entry in one of the panel's callWS / subscribe objects.
SENT_TYPE = re.compile(r"""type:\s*["'](nova/[a-z0-9_]+)["']""")

# Commands the panel cannot live without; if extraction misses one of these
# the parsing below has gone wrong, whatever the totals say.
ANCHOR_COMMANDS = ("nova/get_panel_data", "nova/update_config",
                   "nova/forget_knowledge", "nova/chat")


def _is_type_key(node) -> bool:
    """True for `"type"` or `vol.Required("type")` used as a schema key."""
    if isinstance(node, ast.Constant):
        return node.value == "type"
    return (isinstance(node, ast.Call) and bool(node.args)
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "type")


def _command_type(decorator) -> str | None:
    """The command string of a websocket_command({...}) decorator, else None."""
    if not isinstance(decorator, ast.Call) or not decorator.args:
        return None
    schema = decorator.args[0]
    if not isinstance(schema, ast.Dict):
        return None
    for key, value in zip(schema.keys, schema.values):
        if _is_type_key(key) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def declared_handlers() -> dict[str, str]:
    """Handler function name -> the command type its decorator declares."""
    found: dict[str, str] = {}
    for node in ast.walk(_parsed_tree()):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            command = _command_type(decorator)
            if command:
                found[node.name] = command
    return found


def registration_calls() -> list[str]:
    """Every function passed to async_register_command, in source order and
    with repeats kept, read from the syntax tree."""
    names = []
    for node in ast.walk(_parsed_tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "async_register_command"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Name)):
            names.append(node.args[1].id)
    return names


def registration_calls_by_text() -> set[str]:
    """The same list found with a plain text search, as a cross check on the
    syntax tree walk (a call written in an unusual shape would differ)."""
    return set(re.findall(r"async_register_command\(\s*hass\s*,\s*([A-Za-z_]\w*)", ws_text()))


def registered_commands() -> set[str]:
    handlers = declared_handlers()
    return {handlers[name] for name in registration_calls() if name in handlers}


def panel_commands() -> set[str]:
    return set(SENT_TYPE.findall(PANEL_SOURCE.read_text(encoding="utf-8")))


# ── the panel against the backend ────────────────────────────────────────

def test_the_panel_only_sends_commands_the_backend_registers():
    unknown = sorted(panel_commands() - registered_commands())
    assert unknown == [], f"panel calls commands nobody registers: {unknown}"


def test_each_declared_handler_is_registered():
    registered = set(registration_calls())
    stray = sorted(cmd for name, cmd in declared_handlers().items() if name not in registered)
    assert stray == [], f"handlers declare a command but are never registered: {stray}"


def test_each_registered_function_declares_a_command():
    # Home Assistant rejects a handler without a schema at start-up, which
    # would take the rest of the registration list down with it.
    handlers = declared_handlers()
    bare = sorted(name for name in registration_calls() if name not in handlers)
    assert bare == [], f"registered without a @websocket_command schema: {bare}"


def test_tree_and_text_searches_agree_on_registrations():
    assert set(registration_calls()) == registration_calls_by_text()


# ── shape of the backend's own table ─────────────────────────────────────

def test_no_function_is_registered_twice():
    repeats = [name for name, n in collections.Counter(registration_calls()).items() if n > 1]
    assert repeats == []


def test_no_two_handlers_claim_one_command():
    owners = collections.defaultdict(list)
    for name, command in declared_handlers().items():
        owners[command].append(name)
    clashes = {cmd: names for cmd, names in owners.items() if len(names) > 1}
    assert clashes == {}


def test_every_command_lives_in_the_nova_namespace():
    outside = sorted(c for c in declared_handlers().values() if not c.startswith("nova/"))
    assert outside == []


# ── the extraction itself (stops the checks above passing on nothing) ───

@pytest.mark.parametrize("side, collect", [
    ("backend", registered_commands),
    ("panel", panel_commands),
])
def test_extraction_finds_a_realistic_number_of_commands(side, collect):
    found = collect()
    assert len(found) >= 20, f"{side} extraction looks broken: {len(found)} commands"


@pytest.mark.parametrize("command", ANCHOR_COMMANDS)
def test_core_commands_are_seen_on_both_sides(command):
    assert command in registered_commands()
    assert command in panel_commands()
