"""Phase 3A typed runtime — the unit-level half.

The integration tests (tests/integration/test_runtime_data.py) drive the real
setup and unload path. This file proves the runtime module with fakes, plus
static checks on how __init__.py, proactive_audio.py and services.py wire it:

* NovaRuntime is a slots dataclass with exactly the reviewed fields,
* the compatibility bridge helpers are gone (Phase 3C),
* get_runtime() fails loudly instead of inventing a default,
* clear_runtime() drops runtime_data, idempotently, without touching hass.data,
* setup builds each owner once and releases the runtime on every failure path,
* the proactive-audio objects are NovaRuntime fields (Phase 3B), built lazily
  by proactive_audio.py and never kept in hass.data.

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
    "providers", "runtime_config", "schema_version", "observer_running",
    "intent_router", "state_ledger", "entity_locks", "alert_buffer",
    "audit_running", "provider_holds",
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


# ── NovaRuntime shape ───────────────────────────────────────────────────────

def test_runtime_fields_are_the_reviewed_set(rt):
    assert [f.name for f in dataclasses.fields(rt.NovaRuntime)] == FIELDS


def test_runtime_uses_slots(rt):
    runtime = _runtime(rt)
    assert not hasattr(runtime, "__dict__")
    with pytest.raises(AttributeError):
        runtime.not_a_field = True


def test_runtime_defaults(rt, load):
    a, b = _runtime(rt), _runtime(rt)
    assert a.automation_inventory is None
    assert a.schema_version == load("migrations").CURRENT_SCHEMA_VERSION
    assert a.runtime_config == {}
    assert a.runtime_config is not b.runtime_config   # never shared across entries


# ── No compatibility bridge (Phase 3C) ──────────────────────────────────────

def test_bridge_helpers_are_gone(rt):
    for name in ("build_compat_bridge", "mirror_to_bridge"):
        assert not hasattr(rt, name), name


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

def test_release_drops_runtime_only(rt):
    runtime = _runtime(rt)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime)

    rt.clear_runtime(entry)

    assert not hasattr(entry, "runtime_data")
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.get_runtime(entry)


def test_release_takes_no_hass():
    """clear_runtime() cannot reach hass.data: it is not handed hass."""
    fn = _func(COMP / "runtime.py", "clear_runtime")
    assert [a.arg for a in fn.args.args] == ["entry"]


def test_release_clears_the_provider_holds(rt):
    runtime = _runtime(rt)
    runtime.provider_holds.hold("sig")
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime)
    rt.clear_runtime(entry)
    assert len(runtime.provider_holds) == 0
    rt.clear_runtime(entry)


def test_release_is_idempotent(rt):
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=_runtime(rt))
    rt.clear_runtime(entry)
    rt.clear_runtime(entry)
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
                  "ReminderWatcher", "AutomationContextTracker"):
        assert calls.count(owner) == 1, owner
    assert "build_compat_bridge" not in calls


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


# ── Proactive-audio objects: entry-owned, on NovaRuntime (Phase 3B) ─────────

def test_proactive_audio_objects_default_to_unbuilt(rt):
    runtime = _runtime(rt)
    for name in ("intent_router", "state_ledger", "entity_locks", "alert_buffer"):
        assert getattr(runtime, name) is None, name
    assert runtime.audit_running is False


def test_proactive_audio_objects_live_on_the_runtime():
    src = (COMP / "proactive_audio.py").read_text(encoding="utf-8")
    # No domain-level bucket: nothing reads or writes hass.data here.
    assert "hass.data" not in src
    for key in ("'_intent_router'", "'_state_ledger'", "'_entity_locks'",
                "'_alert_buffer'", "ALERT_BUFFER_KEY", "'_audit_running'"):
        assert key not in src, key
    # Each object is built lazily on its runtime field.
    for field, cls in (("state_ledger", "StateLedger"),
                       ("entity_locks", "EntityLockRegistry"),
                       ("alert_buffer", "AlertBuffer")):
        assert f"if runtime.{field} is None:\n        runtime.{field} = {cls}()" in src, field
    assert "if runtime.intent_router is None:" in src
    # Unload forgets all four.
    unload = ast.unparse(_func(COMP / "proactive_audio.py", "async_unload_proactive_audio"))
    for field in ("intent_router", "state_ledger", "entity_locks", "alert_buffer"):
        assert f"runtime.{field} = None" in unload, field


def test_proactive_audio_service_handlers_resolve_the_entry_runtime():
    """Phase 4: services.py owns the speak / process_intent handlers. Each
    resolves the loaded entry and its runtime per call; proactive_audio.py
    registers nothing and has no domain-level runtime lookup left."""
    reg = _func(COMP / "services.py", "async_setup_services")
    handlers = {n.name: n for n in ast.walk(reg)
                if isinstance(n, ast.AsyncFunctionDef)}
    for name in ("_speak", "_process_intent"):
        body = ast.unparse(handlers[name])
        assert "entry, runtime = async_resolve_loaded(hass)" in body, name
    src = (COMP / "proactive_audio.py").read_text(encoding="utf-8")
    for gone in ("async_register_services", "_service_runtime", "domain_runtime",
                 "async_register(", "async_remove("):
        assert gone not in src, gone


def test_proactive_audio_unsubs_are_owned_by_resources():
    setup = ast.unparse(_func(COMP / "proactive_audio.py", "async_setup_proactive_audio"))
    assert "runtime.resources.add_unsubs(unsubs)" in setup
    assert "runtime = get_runtime(entry)" in setup
    unload = ast.unparse(_func(COMP / "proactive_audio.py", "async_unload_proactive_audio"))
    # Unload never calls them itself: NovaResources already did.
    assert "cancel()" not in unload and "proactive_audio_unsubs" not in unload
