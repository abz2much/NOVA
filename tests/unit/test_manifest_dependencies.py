"""manifest.json against what the code really imports (v6.73.0).

hassfest, run by the HACS validation in CI, fails when the integration imports
a Home Assistant component it has not listed under `dependencies` or
`after_dependencies`. Running the same comparison here makes the mistake show
up on a local pytest run instead of after a push.

Every .py file under the component is scanned, sub-packages included
(agent_runtime/, automation/, cognitive/ and the rest), and imports are read
from the syntax tree so a parenthesised multi-line import is not missed.
"""
from __future__ import annotations

import ast
import functools
import json
import pathlib
import re

import pytest

COMPONENT = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
HA_COMPONENTS = "homeassistant.components"

# Parts of Home Assistant that are always loaded and that hassfest does not
# ask an integration to declare. Kept deliberately short.
ALWAYS_AVAILABLE = frozenset({
    "persistent_notification",   # set up automatically
    "websocket_api",             # core API, never needs declaring
    # Entity platforms (hassfest allows every homeassistant.const.Platform).
    "binary_sensor",
    "sensor",
})

# A dotted mention anywhere in the text, which also catches
# importlib.import_module("homeassistant.components.x") style lookups.
DOTTED_MENTION = re.compile(r"homeassistant\.components\.([a-z_][a-z0-9_]*)")


def _manifest() -> dict:
    return json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))


def declared() -> set[str]:
    data = _manifest()
    return set(data.get("dependencies", [])) | set(data.get("after_dependencies", []))


def _sources():
    return sorted(COMPONENT.rglob("*.py"))


def _from_tree(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == HA_COMPONENTS:
                names |= {alias.name for alias in node.names}
            elif node.module.startswith(HA_COMPONENTS + "."):
                names.add(node.module.split(".")[2])
        elif isinstance(node, ast.Import):
            names |= {alias.name.split(".")[2] for alias in node.names
                      if alias.name.startswith(HA_COMPONENTS + ".")}
    return names


@functools.lru_cache(maxsize=1)
def imported() -> dict[str, frozenset[str]]:
    """Component name -> the files (relative to the component) that use it.
    Parsing the whole component is slow, so the answer is kept for the run."""
    users: dict[str, set[str]] = {}
    for path in _sources():
        source = path.read_text(encoding="utf-8")
        for name in _from_tree(source) | set(DOTTED_MENTION.findall(source)):
            users.setdefault(name, set()).add(str(path.relative_to(COMPONENT)))
    return {name: frozenset(files) for name, files in users.items()}


def test_every_imported_component_is_declared():
    known = declared() | ALWAYS_AVAILABLE
    missing = {name: sorted(files) for name, files in imported().items() if name not in known}
    assert missing == {}, (
        "imported but not declared in manifest dependencies/after_dependencies "
        f"(hassfest will fail); add them to after_dependencies: {missing}")


def test_the_scan_reaches_sub_packages():
    # The scan must look below the top folder; recorder is imported from
    # automation/ as well as from top level modules.
    files = {f for users in imported().values() for f in users}
    assert any("/" in f for f in files)
    assert "automation/patterns.py" in imported()["recorder"]


@pytest.mark.parametrize("component", ["logbook", "recorder"])
def test_history_components_are_declared(component):
    # activity_history reads both.
    assert component in declared()


def test_hard_dependencies_are_not_repeated_as_soft_ones():
    data = _manifest()
    hard = data.get("dependencies", [])
    soft = data.get("after_dependencies", [])
    assert len(hard) == len(set(hard)) and len(soft) == len(set(soft))
    assert set(hard).isdisjoint(soft)


def test_manifest_keys_follow_the_order_hassfest_wants():
    # domain, then name, then everything else alphabetically.
    keys = list(_manifest())
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
    assert _manifest()["domain"] == "nova"
