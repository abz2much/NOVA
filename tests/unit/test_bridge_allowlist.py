"""Phase 3C: Nova keeps no state in hass.data.

NovaRuntime (entry.runtime_data) is the only owner of Nova's runtime state.
The hass.data[DOMAIN][entry_id] compatibility bridge is gone: production code
must never create, read, mirror, update or delete one again.

This test parses every module in the production package and finds every
access to Home Assistant's hass.data (on any object named hass, like
self.hass or _STATE.hass, and getattr(hass, "data")). Nova-owned access must
be empty. The only uses left read other integrations' data; each is listed
below, matched exactly by module, enclosing function and source line, with
the reason it may stay. A new hass.data use, or one moved or rewritten,
fails here until it is reviewed. The list has no patterns and nothing is
skipped silently.

Focused run:
    python -m pytest tests/unit/test_bridge_allowlist.py -q
"""
from __future__ import annotations

import ast
import pathlib
from collections import Counter

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"

# Nova-owned hass.data access. Phase 3C removed the bridge: nothing is left
# and nothing may be added.
NOVA_OWNED_HASS_DATA: dict[tuple[str, str, str], str] = {}

# Other integrations' data, which is not Nova's.
FOREIGN_ALLOWLIST = {
    ("camera.py", "_fetch_nest_event_image", 'nest_data = hass.data.get("nest")'):
        "the Nest integration's own data",
    ("camera_backends.py", "fetch_best_image", 'nest_data = hass.data.get("nest")'):
        "the Nest integration's own data",
    ("doorbell_training.py", "_iter_nest_doorbell_devices",
     'data = (hass.data.get("nest") or {}).get(getattr(entry, "entry_id", ""), None)'):
        "the Nest integration's own data",
    ("automation/inventory.py", "_runtime_entities",
     "component = hass.data.get(DATA_COMPONENT)"):
        "the automation integration's entity component",
}

# Names that existed only for the bridge. None may come back as a function,
# a call or an import.
REMOVED_BRIDGE_NAMES = {"build_compat_bridge", "mirror_to_bridge"}

# Keys only the bridge ever held. No production string subscript or
# .get()/.pop()/.setdefault() of one may exist anywhere, runtime.py included.
BRIDGE_KEYS = {
    "runtime_config", "observer_running", "proactive_audio_unsubs",
    "automation_inventory", "camera_unsubs", "recognition_unsubs",
    "_audit_running", "_intent_router", "_state_ledger", "_entity_locks",
    "_alert_buffer",
}


def _is_hass(node: ast.AST) -> bool:
    """hass, self.hass, _STATE.hass, _MON.hass and so on."""
    if isinstance(node, ast.Name):
        return node.id == "hass"
    return isinstance(node, ast.Attribute) and node.attr in ("hass", "_hass")


def _modules():
    for path in sorted(COMP.rglob("*.py")):
        rel = str(path.relative_to(COMP))
        src = path.read_text(encoding="utf-8")
        yield rel, ast.parse(src), src.splitlines()


def _owners(tree: ast.AST) -> dict[int, str]:
    owner: dict[int, str] = {}

    def visit(node, name):
        for child in ast.iter_child_nodes(node):
            n = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            owner[id(child)] = n
            visit(child, n)
    visit(tree, "<module>")
    return owner


def _hass_data_uses() -> list[tuple[str, str, str]]:
    found = []
    for rel, tree, lines in _modules():
        owner = _owners(tree)
        for node in ast.walk(tree):
            hit = (isinstance(node, ast.Attribute) and node.attr == "data"
                   and _is_hass(node.value))
            hit = hit or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and _is_hass(node.args[0])
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == "data")
            if hit:
                found.append((rel, owner[id(node)], lines[node.lineno - 1].strip()))
    return found


def _is_domain_key(node: ast.AST) -> bool:
    """DOMAIN, const.DOMAIN or the literal "nova"."""
    if isinstance(node, ast.Name):
        return node.id == "DOMAIN"
    if isinstance(node, ast.Attribute):
        return node.attr == "DOMAIN"
    return isinstance(node, ast.Constant) and node.value == "nova"


def test_nova_owned_hass_data_access_is_empty():
    assert NOVA_OWNED_HASS_DATA == {}
    nova_owned = sorted(set(_hass_data_uses()) - set(FOREIGN_ALLOWLIST))
    assert nova_owned == [], (
        "Nova must not keep state in hass.data. Use NovaRuntime "
        "(get_runtime / current_runtime / domain_runtime) instead: "
        f"{nova_owned}")


def test_each_foreign_use_appears_exactly_once():
    counts = Counter(_hass_data_uses())
    for key in FOREIGN_ALLOWLIST:
        assert counts[key] == 1, (key, counts[key])
    assert sum(counts.values()) == len(FOREIGN_ALLOWLIST)


def test_every_foreign_entry_has_a_reason():
    assert len(FOREIGN_ALLOWLIST) == 4
    for key, reason in FOREIGN_ALLOWLIST.items():
        assert isinstance(reason, str) and len(reason) > 10, key


def test_foreign_uses_never_touch_nova_state():
    for (rel, _fn, line) in FOREIGN_ALLOWLIST:
        assert "DOMAIN" not in line.replace("DATA_COMPONENT", "") and '"nova"' not in line, rel


def test_removed_bridge_helpers_stay_removed():
    found = []
    for rel, tree, _lines in _modules():
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.asname or node.name
            if name in REMOVED_BRIDGE_NAMES:
                found.append((rel, name))
    assert found == []


def test_nothing_is_keyed_by_the_nova_domain():
    """No dict lookup, write, pop or membership test keyed by DOMAIN (or
    "nova") survives, whatever the store is called."""
    offenders = []
    for rel, tree, lines in _modules():
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Subscript):
                hit = _is_domain_key(node.slice)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("get", "pop", "setdefault") and node.args):
                hit = _is_domain_key(node.args[0])
            elif isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
                hit = (node.left.id == "DOMAIN"
                       and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops))
            if hit:
                offenders.append((rel, lines[node.lineno - 1].strip()))
    assert offenders == []


def test_no_production_reader_or_writer_of_bridge_keys():
    """No string subscript or .get()/.pop()/.setdefault() of a key only the
    bridge held survives anywhere in the package."""
    offenders = []
    for rel, tree, lines in _modules():
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                key = node.slice.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("get", "pop", "setdefault") and node.args
                  and isinstance(node.args[0], ast.Constant)):
                key = node.args[0].value
            if key in BRIDGE_KEYS:
                offenders.append((rel, lines[node.lineno - 1].strip()))
    assert offenders == []
