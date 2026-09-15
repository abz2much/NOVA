"""Decision browser WS helpers (Phase 1: decision explanations + feedback).

websocket.py needs a real homeassistant.components.websocket_api to import,
which this sandbox doesn't have (see test_websocket_admin_gate.py's own note),
so _bound_decision_strings — a small, dependency-free pure function — is
extracted via the same ast-extraction pattern test_agent_tool_schema.py uses,
rather than importing the whole module. The composed truncate+redact behavior
of the real ws_get_decision handler is proven end-to-end in
tests/integration/test_websocket_security.py, where a real websocket_api is
available.
"""
from __future__ import annotations

import ast
import pathlib
import types

_WS_PY = (pathlib.Path(__file__).resolve().parents[2]
          / "custom_components" / "nova" / "websocket.py")


def _load_bound_decision_strings():
    src = _WS_PY.read_text()
    tree = ast.parse(src)
    mod = types.ModuleType("ws_decisions_stub")
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_DECISION_FIELD_MAX_CHARS"
                for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<ws>", "exec"), mod.__dict__)
        if isinstance(node, ast.FunctionDef) and node.name == "_bound_decision_strings":
            code = ast.get_source_segment(src, node)
            exec(compile(code, "<ws_fn>", "exec"), mod.__dict__)
    return mod.__dict__["_bound_decision_strings"], mod.__dict__["_DECISION_FIELD_MAX_CHARS"]


def test_short_string_passes_through_unchanged():
    bound, _ = _load_bound_decision_strings()
    assert bound("Gym") == "Gym"


def test_oversized_string_is_truncated_with_marker():
    bound, cap = _load_bound_decision_strings()
    long = "x" * (cap + 50)
    out = bound(long)
    assert len(out) == cap + 1  # cap chars + the "…" marker
    assert out.endswith("…")
    assert out[:-1] == long[:cap]


def test_truncation_recurses_into_nested_dicts_and_lists():
    bound, cap = _load_bound_decision_strings()
    long = "y" * (cap + 10)
    out = bound({"a": long, "b": [long, {"c": long}], "d": 3, "e": None})
    assert out["a"].endswith("…") and len(out["a"]) == cap + 1
    assert out["b"][0].endswith("…")
    assert out["b"][1]["c"].endswith("…")
    assert out["d"] == 3 and out["e"] is None  # non-strings pass through untouched


def test_exactly_at_cap_is_not_truncated():
    bound, cap = _load_bound_decision_strings()
    exact = "z" * cap
    assert bound(exact) == exact  # boundary: <= cap is left alone
