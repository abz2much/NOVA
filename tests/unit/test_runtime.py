"""Phase 3A typed runtime — the unit-level half.

The integration tests (tests/integration/test_runtime_data.py) drive the real
setup and unload path. This file proves the runtime module with fakes, plus
static checks on how __init__.py and proactive_audio.py wire it:

* NovaRuntime is a slots dataclass with exactly the reviewed fields,
* the hass.data bridge holds the runtime's own objects (identity, not copies),
* get_runtime() fails loudly instead of inventing a default,
* clear_runtime() drops runtime_data and the bridge, idempotently,
* setup builds each owner once and releases the runtime on every failure path,
* the proactive-audio shared objects stay domain-level and outside the runtime.

Focused run:
    python -m pytest tests/unit/test_runtime.py -q
"""
import ast
import dataclasses
import pathlib
import types

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"

FIELDS = [
    "client", "llm_provider_name", "sentinel", "reminder_watcher",
    "scheduler", "resources", "automation_contexts", "automation_inventory",
    "runtime_config", "schema_version",
]


@pytest.fixture
def rt(load):
    return load("runtime")


def _runtime(rt, **overrides):
    kwargs = dict(
        client=object(), llm_provider_name="ollama", sentinel=object(),
        reminder_watcher=object(), scheduler=object(), resources=object(),
        automation_contexts=object(),
    )
    kwargs.update(overrides)
    return rt.NovaRuntime(**kwargs)


def _hass(store=None):
    return types.SimpleNamespace(data={} if store is None else store)


# ── NovaRuntime shape ───────────────────────────────────────────────────────

def test_runtime_fields_are_the_reviewed_set(rt):
    assert [f.name for f in dataclasses.fields(rt.NovaRuntime)] == FIELDS


def test_runtime_uses_slots(rt):
    runtime = _runtime(rt)
    assert not hasattr(runtime, "__dict__")
    with pytest.raises(AttributeError):
        runtime.observer_running = True   # not a runtime field in Phase 3A


def test_runtime_defaults(rt, load):
    a, b = _runtime(rt), _runtime(rt)
    assert a.automation_inventory is None
    assert a.schema_version == load("migrations").CURRENT_SCHEMA_VERSION
    assert a.runtime_config == {}
    assert a.runtime_config is not b.runtime_config   # never shared across entries


# ── Compatibility bridge ────────────────────────────────────────────────────

def test_bridge_values_are_the_runtime_objects(rt):
    runtime = _runtime(rt)
    cam, rec = [lambda: None], [lambda: None]
    bridge = rt.build_compat_bridge(runtime, camera_unsubs=cam, recognition_unsubs=rec)
    for key in ("client", "sentinel", "reminder_watcher", "scheduler", "resources",
                "automation_contexts", "runtime_config", "llm_provider_name",
                "schema_version"):
        assert bridge[key] is getattr(runtime, key), key
    assert bridge["camera_unsubs"] is cam
    assert bridge["recognition_unsubs"] is rec
    assert "automation_inventory" not in bridge   # absent until built, as before
    assert "observer_running" not in bridge       # setup still writes it itself


def test_bridge_includes_inventory_by_identity_when_built(rt):
    inventory = object()
    runtime = _runtime(rt, automation_inventory=inventory)
    bridge = rt.build_compat_bridge(runtime, camera_unsubs=[], recognition_unsubs=[])
    assert bridge["automation_inventory"] is inventory


def test_panel_write_through_bridge_is_seen_by_runtime(rt):
    """websocket.py writes panel settings with setdefault("runtime_config", {});
    that must land in the runtime's own dict, not a new one."""
    runtime = _runtime(rt)
    bridge = rt.build_compat_bridge(runtime, camera_unsubs=[], recognition_unsubs=[])
    bridge.setdefault("runtime_config", {})["announcement_speakers"] = ["media_player.a"]
    assert runtime.runtime_config == {"announcement_speakers": ["media_player.a"]}
    runtime.runtime_config["proactive_tts_entity"] = "tts.x"
    assert bridge["runtime_config"]["proactive_tts_entity"] == "tts.x"


# ── get_runtime ─────────────────────────────────────────────────────────────

def test_get_runtime_returns_live_runtime(rt):
    runtime = _runtime(rt)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime)
    assert rt.get_runtime(entry) is runtime


@pytest.mark.parametrize("entry", [
    types.SimpleNamespace(entry_id="e1"),
    types.SimpleNamespace(entry_id="e1", runtime_data=None),
    types.SimpleNamespace(entry_id="e1", runtime_data={"client": object()}),
])
def test_get_runtime_raises_when_missing_or_wrong(rt, entry):
    with pytest.raises(rt.NovaRuntimeUnavailable, match="e1"):
        rt.get_runtime(entry)
    assert issubclass(rt.NovaRuntimeUnavailable, RuntimeError)


# ── clear_runtime ───────────────────────────────────────────────────────────

def test_release_drops_runtime_and_bridge_only(rt):
    runtime = _runtime(rt)
    buffer = object()
    other = {"client": object()}
    store = {"e1": rt.build_compat_bridge(runtime, camera_unsubs=[], recognition_unsubs=[]),
             "e2": other, "_alert_buffer": buffer}
    hass = _hass({"nova": store})
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime)

    rt.clear_runtime(hass, entry)

    assert not hasattr(entry, "runtime_data")
    assert "e1" not in store
    assert store["e2"] is other              # another entry's bridge untouched
    assert store["_alert_buffer"] is buffer  # domain-level key untouched
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.get_runtime(entry)


def test_release_is_idempotent(rt):
    hass = _hass({"nova": {}})
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=_runtime(rt))
    rt.clear_runtime(hass, entry)
    rt.clear_runtime(hass, entry)
    rt.clear_runtime(_hass(), entry)       # no nova bucket at all
    assert not hasattr(entry, "runtime_data")


# ── Static wiring checks on __init__.py ─────────────────────────────────────

def _func(path: pathlib.Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def _call_names(node: ast.AST) -> list[str]:
    """Called names: bare for f(), dotted for mod.f() (so cognitive_core's own
    release_runtime() can't be mistaken for runtime.clear_runtime())."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.append(f.id)
            elif isinstance(f, ast.Attribute):
                base = f.value.id if isinstance(f.value, ast.Name) else ""
                out.append(f"{base}.{f.attr}" if base else f.attr)
    return out


def test_setup_constructs_each_owner_once():
    calls = _call_names(_func(COMP / "__init__.py", "async_setup_entry"))
    for owner in ("NovaRuntime", "NovaScheduler", "NovaResources", "NovaSentinel",
                  "ReminderWatcher", "AutomationContextTracker", "build_compat_bridge"):
        assert calls.count(owner) == 1, owner


def test_setup_assigns_runtime_data_once():
    setup = _func(COMP / "__init__.py", "async_setup_entry")
    targets = [t for n in ast.walk(setup) if isinstance(n, ast.Assign)
               for t in n.targets
               if isinstance(t, ast.Attribute) and t.attr == "runtime_data"]
    assert len(targets) == 1


def test_every_setup_failure_path_releases_runtime():
    """Each except-block in setup that runs async_unload_entry must also run
    clear_runtime before re-raising."""
    setup = _func(COMP / "__init__.py", "async_setup_entry")
    handlers = [h for n in ast.walk(setup) if isinstance(n, ast.Try)
                for h in n.handlers if "async_unload_entry" in _call_names(h)]
    assert len(handlers) == 2   # late setup steps + observer start
    for h in handlers:
        order = [name for stmt in h.body for name in _call_names(stmt)
                 if name in ("async_unload_entry", "clear_runtime")]
        assert order == ["async_unload_entry", "clear_runtime"]
        assert isinstance(h.body[-1], ast.Raise)


def test_unload_releases_runtime():
    assert "clear_runtime" in _call_names(_func(COMP / "__init__.py", "async_unload_entry"))


# ── Proactive-audio shared objects: domain-level, outside the runtime ───────

def test_proactive_audio_shared_objects_stay_outside_runtime(rt):
    src = (COMP / "proactive_audio.py").read_text(encoding="utf-8")
    assert "runtime_data" not in src and "NovaRuntime" not in src
    for name in ("intent_router", "state_ledger", "entity_locks", "alert_buffer"):
        assert name not in FIELDS
    # Released when the last entry unloads, exactly as before Phase 3A.
    unload = ast.unparse(_func(COMP / "proactive_audio.py", "async_unload_proactive_audio"))
    for key in ("'_intent_router'", "'_state_ledger'", "'_entity_locks'", "ALERT_BUFFER_KEY"):
        assert f"store.pop({key}, None)" in unload, key
