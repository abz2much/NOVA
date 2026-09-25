"""Phase 3B: no production code reads entry runtime state from the bridge.

NovaRuntime (entry.runtime_data) is authoritative for all entry-scoped live
state. The hass.data[DOMAIN][entry_id] bridge stays until Phase 3C deletes
it, but only as a passive structure: built, kept in step and dropped, never
read by a consumer.

This test parses every module in the production package and finds every
access to Home Assistant's hass.data (on any object named hass, like
self.hass or _STATE.hass, and getattr(hass, "data")). Each one must be on
the short allowlist below, matched exactly by module, enclosing function
and source line, with the reason it may stay. A new bridge lookup, or one
moved or rewritten, fails here until it is migrated or reviewed. The list
has no patterns and nothing is skipped silently.

Focused run:
    python -m pytest tests/unit/test_bridge_allowlist.py -q
"""
from __future__ import annotations

import ast
import pathlib
from collections import Counter

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"

# (module, enclosing function, stripped source line): reason.
BRIDGE_ALLOWLIST = {
    # ── Bridge construction ──
    ("__init__.py", "async_setup_entry", "hass.data.setdefault(DOMAIN, {})"):
        "construction: the domain bucket the bridge lives in",
    ("__init__.py", "async_setup_entry",
     "hass.data[DOMAIN][entry.entry_id] = build_compat_bridge("):
        "construction: stores the bridge built from the new runtime",
    # ── Bridge synchronization until Phase 3C ──
    ("__init__.py", "async_setup_entry",
     'hass.data[DOMAIN][entry.entry_id]["automation_inventory"] = automation_inventory'):
        "synchronization: the bridge keeps the inventory key; the runtime owns it",
    ("runtime.py", "set_observer_running", "store = hass.data.get(DOMAIN)"):
        "synchronization: mirrors the runtime's observer_running into the bridge",
    ("runtime.py", "mirror_to_bridge", "store = hass.data.get(DOMAIN)"):
        "synchronization: copies a runtime-owned value into the bridge, write only",
    # ── Lifecycle cleanup ──
    ("runtime.py", "clear_runtime", "store = hass.data.get(DOMAIN)"):
        "lifecycle cleanup: drops the entry's bridge with its runtime",
    # ── Narrow partial/legacy unload fallback ──
    ("__init__.py", "async_unload_entry", "store = hass.data.get(DOMAIN)"):
        "unload fallback: only when the entry has no runtime (partial setup)",
}

# Other integrations' data, which is not Nova's and not the bridge.
FOREIGN_ALLOWLIST = {
    ("camera.py", "_fetch_nest_event_image", 'nest_data = hass.data.get("nest")'):
        "the Nest integration's own data",
    ("camera_backends.py", "fetch_best_image", 'nest_data = hass.data.get("nest")'):
        "the Nest integration's own data",
    ("doorbell_training.py", "_iter_nest_doorbell_devices",
     'data = (hass.data.get("nest") or {}).get(getattr(entry, "entry_id", ""), None)'):
        "the Nest integration's own data",
    ("automation_inventory.py", "_runtime_entities",
     "component = hass.data.get(DATA_COMPONENT)"):
        "the automation integration's entity component",
}

# Every mirror_to_bridge() call: (module, enclosing function, key).
MIRROR_ALLOWLIST = {
    ("proactive_audio.py", "async_setup_proactive_audio", "proactive_audio_unsubs"):
        "the bridge keeps its proactive_audio_unsubs key; NovaResources owns them",
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


def test_every_hass_data_use_is_allowlisted():
    uses = _hass_data_uses()
    allowed = set(BRIDGE_ALLOWLIST) | set(FOREIGN_ALLOWLIST)
    unapproved = sorted(set(uses) - allowed)
    assert unapproved == [], (
        "hass.data use outside the Phase 3B allowlist. Read NovaRuntime "
        "(get_runtime / current_runtime / domain_runtime) instead: "
        f"{unapproved}")


def test_each_allowlisted_use_appears_exactly_once():
    counts = Counter(_hass_data_uses())
    for key in list(BRIDGE_ALLOWLIST) + list(FOREIGN_ALLOWLIST):
        assert counts[key] == 1, (key, counts[key])


def test_allowlist_is_short_and_every_entry_has_a_reason():
    assert len(BRIDGE_ALLOWLIST) <= 8
    for table in (BRIDGE_ALLOWLIST, FOREIGN_ALLOWLIST, MIRROR_ALLOWLIST):
        for key, reason in table.items():
            assert isinstance(reason, str) and len(reason) > 10, key
    kinds = {r.split(":")[0] for r in BRIDGE_ALLOWLIST.values()}
    assert kinds <= {"construction", "synchronization", "lifecycle cleanup",
                     "unload fallback"}


def test_foreign_uses_never_touch_nova_state():
    for (rel, _fn, line) in FOREIGN_ALLOWLIST:
        assert "DOMAIN" not in line.replace("DATA_COMPONENT", "") and '"nova"' not in line, rel


def test_mirror_to_bridge_callers_are_allowlisted():
    calls = []
    for rel, tree, _lines in _modules():
        owner = _owners(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "mirror_to_bridge"):
                key = node.args[2]
                assert isinstance(key, ast.Constant), (rel, "key must be a literal")
                calls.append((rel, owner[id(node)], key.value))
    assert sorted(calls) == sorted(MIRROR_ALLOWLIST)


def test_no_production_reader_of_bridge_runtime_keys():
    """No string subscript or .get() of a bridge-only runtime key survives
    outside runtime.py (the bridge's own module)."""
    keys = {"runtime_config", "observer_running", "proactive_audio_unsubs",
            "_audit_running", "_intent_router", "_state_ledger", "_entity_locks",
            "_alert_buffer", "camera_unsubs", "recognition_unsubs"}
    offenders = []
    for rel, tree, lines in _modules():
        if rel == "runtime.py":
            continue
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                key = node.slice.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("get", "pop", "setdefault") and node.args
                  and isinstance(node.args[0], ast.Constant)):
                key = node.args[0].value
            if key in keys:
                offenders.append((rel, lines[node.lineno - 1].strip()))
    # Legacy unload fallback in __init__.py reads what a partial bridge holds.
    assert sorted(offenders) == sorted([
        ("__init__.py", 'for unsub in data.get("camera_unsubs", []):'),
        ("__init__.py", 'for unsub in data.get("recognition_unsubs", []):'),
        ("__init__.py", 'observer_running = bool(data.get("observer_running"))'),
    ])
