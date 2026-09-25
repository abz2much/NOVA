"""runtime_config_snapshot(): the executor-boundary copy of runtime_config.

NovaRuntime.runtime_config is owned by the event loop and changed in place
by panel writes. Code that hands config to an executor job must pass a
fresh shallow copy taken on the loop, never the live dict. This file proves
the helper with fakes, plus static checks that every executor crossing in
__init__.py and conversation.py passes a snapshot, taken in the same
function right before the job:

* the snapshot is equal to the live dict but never the same object,
* each call makes a new one, so later changes show on the next operation,
* ownership is checked like the live accessors (strict or lifecycle rules),
* it never reads the hass.data bridge.

The blocking-executor proofs run against a real Home Assistant in
tests/integration/test_runtime_config_snapshot.py.

Focused run:
    python -m pytest tests/unit/test_runtime_config_snapshot.py -q
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


def _entry(rt, state=_State.LOADED, runtime_config=None, with_runtime=True):
    entry = types.SimpleNamespace(entry_id="e1", state=state)
    if with_runtime:
        entry.runtime_data = rt.NovaRuntime(
            client=object(), llm_provider_name="ollama", sentinel=object(),
            reminder_watcher=object(), scheduler=object(), resources=object(),
            automation_contexts=object(),
            runtime_config={} if runtime_config is None else runtime_config,
        )
    return entry


# ── The helper ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("strict", [False, True])
def test_snapshot_is_equal_but_not_identical(rt, strict):
    entry = _entry(rt, runtime_config={"model": "m", "pairs": {"a": "b"}})
    live = entry.runtime_data.runtime_config
    snap = rt.runtime_config_snapshot(entry, strict=strict)
    assert snap == live
    assert snap is not live
    assert type(snap) is dict


@pytest.mark.parametrize("strict", [False, True])
def test_each_call_is_a_fresh_snapshot_with_current_values(rt, strict):
    entry = _entry(rt, runtime_config={"model": "first"})
    live = entry.runtime_data.runtime_config
    first = rt.runtime_config_snapshot(entry, strict=strict)
    live["model"] = "second"
    live["added"] = 1
    assert first == {"model": "first"}           # coherent, unchanged
    second = rt.runtime_config_snapshot(entry, strict=strict)
    assert second == {"model": "second", "added": 1}
    assert second is not first


def test_snapshot_changes_do_not_reach_the_live_dict(rt):
    entry = _entry(rt, runtime_config={"model": "live"})
    snap = rt.runtime_config_snapshot(entry)
    snap["model"] = "changed"
    snap["new"] = 1
    assert entry.runtime_data.runtime_config == {"model": "live"}


def test_strict_raises_without_runtime_in_any_state(rt):
    for state in _State:
        with pytest.raises(rt.NovaRuntimeUnavailable):
            rt.runtime_config_snapshot(
                _entry(rt, state=state, with_runtime=False), strict=True)


def test_lifecycle_rules_without_runtime(rt):
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.runtime_config_snapshot(_entry(rt, with_runtime=False))
    for state in (s for s in _State if s is not _State.LOADED):
        snap = rt.runtime_config_snapshot(_entry(rt, state=state, with_runtime=False))
        assert snap == {}


def test_helper_never_reads_the_bridge():
    tree = ast.parse((COMP / "runtime.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "runtime_config_snapshot")
    code = fn.body[1:] if ast.get_docstring(fn) else fn.body
    src = ast.unparse(ast.Module(body=code, type_ignores=[]))
    assert "hass" not in src and "DOMAIN" not in src
    assert src.count("dict(") == 2
    assert "executor" in ast.get_docstring(fn)


# ── Every executor crossing passes a fresh snapshot ─────────────────────────

def _functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _own_nodes(fn):
    """fn's nodes, without those of functions nested inside it."""
    stack, out = list(ast.iter_child_nodes(fn)), []
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _runtime_executor_jobs(path):
    """(function, call) for every executor job fed effective_config_with_runtime."""
    jobs = []
    for fn in _functions(path):
        for n in _own_nodes(fn):
            if (isinstance(n, ast.Call)
                    and ast.unparse(n.func).endswith("async_add_executor_job")
                    and n.args
                    and ast.unparse(n.args[0]).endswith("effective_config_with_runtime")):
                jobs.append((fn, n))
    return jobs


EXPECTED = {
    "__init__.py": {"_host_health_tick", "_sleep_prompt_tick", "async_setup_entry"},
    "conversation.py": {"_handle_message_impl"},
}


@pytest.mark.parametrize("filename", sorted(EXPECTED))
def test_executor_crossings_are_the_reviewed_set(filename):
    jobs = _runtime_executor_jobs(COMP / filename)
    assert {fn.name for fn, _ in jobs} == EXPECTED[filename]
    assert len(jobs) == len(EXPECTED[filename])


@pytest.mark.parametrize("filename", sorted(EXPECTED))
def test_executor_crossings_pass_a_snapshot_taken_right_before(filename):
    for fn, call in _runtime_executor_jobs(COMP / filename):
        arg = call.args[2]
        if isinstance(arg, ast.Call):
            # Taken inline, in the job's own argument list.
            assert ast.unparse(arg.func) == "runtime_config_snapshot", fn.name
            continue
        assert isinstance(arg, ast.Name), fn.name
        assigns = [n for n in _own_nodes(fn) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == arg.id
                           for t in n.targets) and n.lineno < call.lineno]
        assert assigns, fn.name
        last = max(assigns, key=lambda n: n.lineno)
        assert isinstance(last.value, ast.Call), fn.name
        assert ast.unparse(last.value.func) == "runtime_config_snapshot", fn.name
        # Nothing else happens between the snapshot and the job.
        assert call.lineno - last.end_lineno <= 1, fn.name


@pytest.mark.parametrize("filename", sorted(EXPECTED))
def test_no_live_runtime_dict_is_handed_to_an_executor(filename):
    for fn in _functions(COMP / filename):
        for n in _own_nodes(fn):
            if (isinstance(n, ast.Call)
                    and ast.unparse(n.func).endswith("async_add_executor_job")):
                src = ast.unparse(n)
                assert "lifecycle_runtime_config" not in src, fn.name
                assert ".runtime_config" not in src, fn.name
                assert "_runtime_config()" not in src, fn.name
