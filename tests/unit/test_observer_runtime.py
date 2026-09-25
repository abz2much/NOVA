"""Phase 3B.1 observer state on NovaRuntime — the unit-level half.

The integration tests (tests/integration/test_observer_runtime.py) drive the
real setup, services, WebSocket commands and unload. This file proves the
runtime helpers with fakes, plus a static check over the whole package:

* NovaRuntime starts with observer_running False,
* set_observer_running() writes only the runtime, never hass.data,
* it refuses to run without a runtime and never invents one,
* observer_status() reads the runtime, reads False only for an entry that is
  not loaded, and raises for a loaded entry with no runtime,
* lifecycle_runtime() returns None instead of raising,
* no production code writes observer_running outside the one helper.

Focused run:
    python -m pytest tests/unit/test_observer_runtime.py -q
"""
import ast
import enum
import pathlib
import sys
import types

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


class _State(enum.Enum):
    """Stand-in for homeassistant.config_entries.ConfigEntryState."""
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"
    SETUP_IN_PROGRESS = "setup_in_progress"
    SETUP_ERROR = "setup_error"


@pytest.fixture
def rt(load, monkeypatch):
    monkeypatch.setattr(sys.modules["homeassistant.config_entries"],
                        "ConfigEntryState", _State, raising=False)
    return load("runtime")


def _runtime(rt, **overrides):
    kwargs = dict(
        client=object(), llm_provider_name="ollama", sentinel=object(),
        reminder_watcher=object(), scheduler=object(), resources=object(),
        automation_contexts=object(),
    )
    kwargs.update(overrides)
    return rt.NovaRuntime(**kwargs)


def _loaded(rt, state=_State.LOADED):
    """A runtime, a fake hass with empty hass.data, and the entry holding it."""
    runtime = _runtime(rt)
    hass = types.SimpleNamespace(data={})
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime, state=state)
    return hass, entry, runtime


# ── Initial state ───────────────────────────────────────────────────────────

def test_runtime_starts_with_observer_stopped(rt):
    assert _runtime(rt).observer_running is False


# ── set_observer_running ────────────────────────────────────────────────────

def test_set_updates_runtime_on_every_transition(rt):
    hass, entry, runtime = _loaded(rt)
    for running in (True, False, True, True, False):
        rt.set_observer_running(entry, running)
        assert runtime.observer_running is running
    assert hass.data == {}   # never writes Nova state into hass.data


def test_set_stores_a_real_bool(rt):
    _, entry, runtime = _loaded(rt)
    rt.set_observer_running(entry, 1)
    assert runtime.observer_running is True


def test_set_takes_no_hass():
    """Without hass the helper has no way to reach hass.data."""
    src = (COMP / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "set_observer_running")
    assert [a.arg for a in fn.args.args] == ["entry", "running"]
    assert "hass" not in ast.get_source_segment(src, fn)


def test_set_without_runtime_raises_and_invents_nothing(rt):
    entry = types.SimpleNamespace(entry_id="e1", state=_State.LOADED)
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.set_observer_running(entry, True)
    assert not hasattr(entry, "runtime_data")


# ── observer_status ─────────────────────────────────────────────────────────

def test_status_reads_runtime(rt):
    _, entry, runtime = _loaded(rt)
    runtime.observer_running = True
    assert rt.observer_status(entry) is True


@pytest.mark.parametrize("state", [
    _State.NOT_LOADED, _State.SETUP_IN_PROGRESS, _State.SETUP_ERROR])
def test_status_without_runtime_is_false_when_not_loaded(rt, state):
    entry = types.SimpleNamespace(entry_id="e1", state=state)
    assert rt.observer_status(entry) is False


def test_status_without_runtime_raises_when_loaded(rt):
    entry = types.SimpleNamespace(entry_id="e1", state=_State.LOADED)
    with pytest.raises(rt.NovaRuntimeUnavailable, match="e1"):
        rt.observer_status(entry)


def test_status_with_wrong_runtime_type_raises_when_loaded(rt):
    entry = types.SimpleNamespace(entry_id="e1", state=_State.LOADED,
                                  runtime_data={"observer_running": True})
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.observer_status(entry)


# ── lifecycle_runtime ───────────────────────────────────────────────────────

def test_lifecycle_runtime(rt):
    runtime = _runtime(rt)
    assert rt.lifecycle_runtime(types.SimpleNamespace(runtime_data=runtime)) is runtime
    assert rt.lifecycle_runtime(types.SimpleNamespace()) is None
    assert rt.lifecycle_runtime(types.SimpleNamespace(runtime_data=None)) is None
    assert rt.lifecycle_runtime(types.SimpleNamespace(runtime_data={})) is None


# ── Static: one writer ──────────────────────────────────────────────────────

def _observer_running_writes(path: pathlib.Path) -> list[tuple[str, int]]:
    """(enclosing function, line) of every write to observer_running:
    x.observer_running = …, x["observer_running"] = …, and augmented or
    annotated forms of both."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []

    def _is_target(t):
        if isinstance(t, ast.Attribute) and t.attr == "observer_running":
            return True
        return (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                and t.slice.value == "observer_running")

    def _visit(node, func):
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            targets = []
            if isinstance(child, ast.Assign):
                targets = child.targets
            elif isinstance(child, ast.AugAssign | ast.AnnAssign):
                targets = [child.target]
            if any(_is_target(t) for t in targets):
                out.append((func, child.lineno))
            _visit(child, name)

    _visit(tree, "<module>")
    return out


def test_only_the_helper_writes_observer_running():
    writes = {}
    for path in sorted(COMP.rglob("*.py")):
        found = _observer_running_writes(path)
        if found:
            writes[str(path.relative_to(COMP))] = [f for f, _ in found]
    assert writes == {"runtime.py": ["set_observer_running"]}


def test_every_known_writer_uses_the_helper():
    init_src = (COMP / "__init__.py").read_text(encoding="utf-8")
    svc_src = (COMP / "services.py").read_text(encoding="utf-8")
    ws_src = (COMP / "websocket.py").read_text(encoding="utf-8")
    # setup enabled + setup failure, and unload
    assert init_src.count("set_observer_running(entry, True)") == 2
    assert init_src.count("set_observer_running(entry, False)") == 1
    # nova.observer_start and nova.observer_stop (services.py since Phase 4)
    assert svc_src.count("set_observer_running(entry, True)") == 1
    assert svc_src.count("set_observer_running(entry, False)") == 1
    # nova/update_config observer_enabled on and off
    assert ws_src.count("set_observer_running(entry, True)") == 1
    assert ws_src.count("set_observer_running(entry, False)") == 1


def test_panel_status_reads_the_runtime():
    ws_src = (COMP / "websocket.py").read_text(encoding="utf-8")
    assert "observer_running = observer_status(entry)" in ws_src
    assert 'data.get("observer_running"' not in ws_src


def _nested(path: pathlib.Path, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _is_call(node: ast.AST, dotted: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name):
        return f.id == dotted
    return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
            and f"{f.value.id}.{f.attr}" == dotted)


def _is_ownership_check(stmt: ast.stmt) -> bool:
    """A bare get_runtime(...) statement, or the Phase 4 service resolver
    `entry, runtime = async_resolve_loaded(hass)` (which ends in
    get_runtime and raises before returning without a runtime)."""
    if isinstance(stmt, ast.Expr) and _is_call(stmt.value, "get_runtime"):
        return True
    return (isinstance(stmt, ast.Assign)
            and _is_call(stmt.value, "async_resolve_loaded"))


def _guarded_by_get_runtime(func: ast.AST, target: ast.Call) -> bool:
    """True when an ownership check (_is_ownership_check) runs on every path
    before target: it sits earlier in target's own block or in an enclosing
    block, not inside a sibling branch."""
    parents = {c: p for p in ast.walk(func) for c in ast.iter_child_nodes(p)}
    node = target
    while node is not func:
        parent = parents[node]
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and node in block:
                for stmt in block[:block.index(node)]:
                    if _is_ownership_check(stmt):
                        return True
        node = parent
    return False


@pytest.mark.parametrize("path,func,expected", [
    ("services.py", "_observer_start", 1),
    ("services.py", "_observer_stop", 1),
    ("websocket.py", "ws_update_config", 2),   # toggle on and off
])
def test_ownership_is_checked_before_any_observer_change(path, func, expected):
    """All four live paths (both services, the panel toggle on and off)
    resolve the runtime before observer.start()/stop() can run."""
    fn = _nested(COMP / path, func)
    changes = [n for n in ast.walk(fn)
               if _is_call(n, "observer_mod.start") or _is_call(n, "observer_mod.stop")]
    assert len(changes) == expected
    for call in changes:
        assert _guarded_by_get_runtime(fn, call), (func, call.lineno)
