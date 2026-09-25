"""Phase 3B.2 __init__.py runtime_config readers on NovaRuntime — the
unit-level half.

The integration tests (tests/integration/test_init_runtime_consumers.py)
drive the real setup, event listeners, scheduled ticks and services. This
file proves the accessor with fakes, plus static checks over __init__.py:

* lifecycle_runtime_config() returns the runtime's own dict, never a copy,
  and never reads the hass.data bridge,
* it gives {} only for an entry that is not loaded, and raises for a loaded
  entry with no runtime,
* every migrated reader (in __init__.py, or services.py since Phase 4)
  calls the accessor inside its own body, so each call reads live values
  and nothing captures a copy at setup,
* no runtime_config read through hass.data remains in __init__.py, and the
  remaining hass.data uses are exactly the approved bridge and lifecycle ones.

Focused run:
    python -m pytest tests/unit/test_init_runtime_consumers.py -q
"""
import ast
import enum
import pathlib
import sys
import types

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
INIT = COMP / "__init__.py"
# Phase 4 moved the service handlers and _get_speakers into services.py.
SERVICES = COMP / "services.py"


class _State(enum.Enum):
    """Stand-in for homeassistant.config_entries.ConfigEntryState."""
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"
    SETUP_IN_PROGRESS = "setup_in_progress"
    SETUP_ERROR = "setup_error"
    SETUP_RETRY = "setup_retry"
    FAILED_UNLOAD = "failed_unload"


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


# ── lifecycle_runtime_config() ──────────────────────────────────────────────

@pytest.mark.parametrize("state", list(_State))
def test_returns_the_runtime_dict_itself_in_any_state(rt, state):
    runtime = _runtime(rt, runtime_config={"camera_auto_analyze": False})
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime, state=state)
    assert rt.lifecycle_runtime_config(entry) is runtime.runtime_config


def test_in_place_changes_are_seen_on_the_next_call(rt):
    runtime = _runtime(rt)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime,
                                  state=_State.LOADED)
    assert rt.lifecycle_runtime_config(entry) == {}
    runtime.runtime_config["announcement_speakers"] = ["media_player.a"]
    assert rt.lifecycle_runtime_config(entry)["announcement_speakers"] == [
        "media_player.a"]


def test_ignores_a_drifted_or_missing_bridge(rt):
    """The accessor takes no hass at all, so no bridge can reach it."""
    runtime = _runtime(rt, runtime_config={"k": "runtime"})
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=runtime,
                                  state=_State.LOADED)
    import inspect
    assert list(inspect.signature(rt.lifecycle_runtime_config).parameters) == ["entry"]
    assert rt.lifecycle_runtime_config(entry) == {"k": "runtime"}


@pytest.mark.parametrize("state", [s for s in _State if s is not _State.LOADED])
def test_missing_runtime_is_empty_when_not_loaded(rt, state):
    entry = types.SimpleNamespace(entry_id="e1", state=state)
    assert rt.lifecycle_runtime_config(entry) == {}


def test_missing_runtime_raises_when_loaded(rt):
    entry = types.SimpleNamespace(entry_id="e1", state=_State.LOADED)
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.lifecycle_runtime_config(entry)


def test_wrong_runtime_type_raises_when_loaded(rt):
    entry = types.SimpleNamespace(entry_id="e1", state=_State.LOADED,
                                  runtime_data={"runtime_config": {"k": 1}})
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.lifecycle_runtime_config(entry)


def test_accessor_never_reads_hass_data():
    tree = ast.parse((COMP / "runtime.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "lifecycle_runtime_config")
    code = fn.body[1:] if ast.get_docstring(fn) else fn.body
    src = ast.unparse(ast.Module(body=code, type_ignores=[]))
    assert "hass" not in src and "DOMAIN" not in src
    assert "dict(" not in src and ".copy(" not in src


# ── Static checks over __init__.py and services.py ──────────────────────────

_TREE = ast.parse(INIT.read_text(encoding="utf-8"))
_SERVICES_TREE = ast.parse(SERVICES.read_text(encoding="utf-8"))


def _func(name: str) -> ast.AST:
    found = [n for tree in (_TREE, _SERVICES_TREE) for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == name]
    assert len(found) == 1, (name, len(found))
    return found[0]


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name) and c.func.id == name]


def _hass_data_nodes(node: ast.AST) -> list[ast.Attribute]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Attribute)
            and n.attr == "data" and isinstance(n.value, ast.Name)
            and n.value.id == "hass"]


MIGRATED = ("_auto_flag", "_host_health_tick", "_sleep_prompt_tick",
            "_get_speakers", "_test_notify", "_test_routing")

# The ticks hand their config to an executor job, so they take a fresh
# snapshot (Phase 3B.3); the synchronous readers use the live dict.
EXECUTOR_READERS = ("_host_health_tick", "_sleep_prompt_tick")


def _accessor(name: str) -> str:
    return ("runtime_config_snapshot" if name in EXECUTOR_READERS
            else "lifecycle_runtime_config")


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_reader_uses_the_runtime_accessor(name):
    fn = _func(name)
    assert _calls(fn, _accessor(name)), name
    assert not _hass_data_nodes(fn), name


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_reader_reads_on_every_call(name):
    """The accessor runs inside the function's own body (each event, tick or
    service call), not once in an enclosing scope that could hold a copy."""
    fn = _func(name)
    for call in _calls(fn, _accessor(name)):
        assert call.args and isinstance(call.args[0], ast.Name)
        assert call.args[0].id == "entry"
    inner = [n for n in ast.walk(fn) if n is not fn and isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
    for sub in inner:
        assert not _calls(sub, _accessor(name))


def test_missing_runtime_escapes_speaker_and_flag_fallbacks():
    """_auto_flag and _get_speakers wrap their parsing in broad try/except.
    The runtime read must sit outside those blocks, or a loaded entry with no
    runtime would be swallowed into defaults."""
    for name in ("_auto_flag", "_get_speakers", "_test_routing"):
        fn = _func(name)
        for t in (n for n in ast.walk(fn) if isinstance(n, ast.Try)):
            for stmt in t.body:
                assert not _calls(stmt, "lifecycle_runtime_config"), name


def test_ticks_report_missing_runtime_as_a_warning():
    for name in ("_host_health_tick", "_sleep_prompt_tick"):
        fn = _func(name)
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
        assert len(tries) == 1
        handlers = tries[0].handlers
        assert ast.unparse(handlers[0].type) == "NovaRuntimeUnavailable"
        assert "_LOGGER.warning" in ast.unparse(handlers[0])
        # The broad handler is still there and still debug-only.
        assert ast.unparse(handlers[-1].type) == "Exception"
        assert "_LOGGER.debug" in ast.unparse(handlers[-1])


def test_lockdown_reads_the_strict_runtime_inside_its_non_fatal_block():
    setup = _func("async_setup_entry")
    lockdown_try = next(
        t for t in ast.walk(setup) if isinstance(t, ast.Try)
        and "ensure_lockdown" in ast.unparse(ast.Module(body=t.body, type_ignores=[])))
    body = ast.unparse(ast.Module(body=lockdown_try.body, type_ignores=[]))
    assert "runtime_config_snapshot(entry, strict=True)" in body
    assert "hass.data" not in body
    assert [ast.unparse(h.type) for h in lockdown_try.handlers] == ["Exception"]
    assert "_LOGGER.warning" in ast.unparse(lockdown_try.handlers[0])


def test_test_notify_merges_data_then_options_then_runtime():
    fn = _func("_test_notify")
    src = ast.unparse(fn)
    order = [src.index(s) for s in (
        "notify_config = dict(entry.data)",
        "notify_config.update(entry.options)",
        "notify_config.update(rc)")]
    assert order == sorted(order)


def test_no_runtime_config_is_read_through_hass_data():
    """runtime_config is only ever reached as runtime.runtime_config."""
    consts = [n for tree in (_TREE, _SERVICES_TREE) for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and n.value == "runtime_config"]
    assert consts == []


def test_init_has_no_hass_data_access():
    """Phase 3C: __init__.py neither creates, writes, reads nor deletes
    anything in hass.data. Setup stores state only on entry.runtime_data.
    Phase 4: nor does services.py."""
    assert list(_hass_data_nodes(_TREE)) == []
    assert list(_hass_data_nodes(_SERVICES_TREE)) == []
