"""Phase 3B.1 observer state on NovaRuntime — the unit-level half.

The integration tests (tests/integration/test_observer_runtime.py) drive the
real setup, services, WebSocket commands and unload. This file proves the
runtime helpers with fakes, plus a static check over the whole package:

* NovaRuntime starts with observer_running False, and the bridge copies it,
* set_observer_running() writes the runtime first and mirrors the bridge,
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
    """A runtime, its bridge in a fake hass, and the entry holding both."""
    runtime = _runtime(rt)
    bridge = rt.build_compat_bridge(runtime, camera_unsubs=[], recognition_unsubs=[])
    hass = types.SimpleNamespace(data={"nova": {"e1": bridge}})
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime, state=state)
    return hass, entry, runtime, bridge


# ── Initial state ───────────────────────────────────────────────────────────

def test_runtime_starts_with_observer_stopped(rt):
    assert _runtime(rt).observer_running is False


def test_bridge_is_seeded_from_runtime(rt):
    _, _, runtime, bridge = _loaded(rt)
    assert bridge["observer_running"] is False is runtime.observer_running


# ── set_observer_running ────────────────────────────────────────────────────

def test_set_updates_runtime_and_bridge_on_every_transition(rt):
    hass, entry, runtime, bridge = _loaded(rt)
    for running in (True, False, True, True, False):
        rt.set_observer_running(hass, entry, running)
        assert runtime.observer_running is running
        assert bridge["observer_running"] is running


def test_set_stores_a_real_bool(rt):
    hass, entry, runtime, bridge = _loaded(rt)
    rt.set_observer_running(hass, entry, 1)
    assert runtime.observer_running is True and bridge["observer_running"] is True


@pytest.mark.parametrize("store", [
    {},                          # no nova bucket
    {"nova": {}},                # bucket without this entry's bridge
    {"nova": {"e1": None}},      # damaged bridge
    {"nova": "not a dict"},      # damaged bucket
])
def test_set_without_usable_bridge_still_updates_runtime(rt, store):
    runtime = _runtime(rt)
    hass = types.SimpleNamespace(data=store)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime, state=_State.LOADED)
    rt.set_observer_running(hass, entry, True)
    assert runtime.observer_running is True
    assert "e1" not in store  # never recreates a bridge


def test_set_without_runtime_raises_and_leaves_bridge_alone(rt):
    bridge = {"observer_running": False}
    hass = types.SimpleNamespace(data={"nova": {"e1": bridge}})
    entry = types.SimpleNamespace(entry_id="e1", state=_State.LOADED)
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.set_observer_running(hass, entry, True)
    assert bridge == {"observer_running": False}
    assert not hasattr(entry, "runtime_data")


def test_other_entries_bridge_is_untouched(rt):
    hass, entry, _, _ = _loaded(rt)
    other = {"observer_running": False}
    hass.data["nova"]["e2"] = other
    rt.set_observer_running(hass, entry, True)
    assert other == {"observer_running": False}


# ── observer_status ─────────────────────────────────────────────────────────

def test_status_reads_runtime_not_bridge(rt):
    hass, entry, runtime, bridge = _loaded(rt)
    runtime.observer_running = True      # deliberate drift, test only
    bridge["observer_running"] = False
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
    assert writes == {"runtime.py": ["set_observer_running", "set_observer_running"]}


def test_every_known_writer_uses_the_helper():
    init_src = (COMP / "__init__.py").read_text(encoding="utf-8")
    ws_src = (COMP / "websocket.py").read_text(encoding="utf-8")
    # setup enabled + setup failure + observer_start + observer_stop + unload
    assert init_src.count("set_observer_running(hass, entry, True)") == 3
    assert init_src.count("set_observer_running(hass, entry, False)") == 2
    # nova/update_config observer_enabled on and off
    assert ws_src.count("set_observer_running(hass, entry, True)") == 1
    assert ws_src.count("set_observer_running(hass, entry, False)") == 1


def test_panel_status_reads_the_runtime():
    ws_src = (COMP / "websocket.py").read_text(encoding="utf-8")
    assert "observer_running = observer_status(entry)" in ws_src
    assert 'data.get("observer_running"' not in ws_src
