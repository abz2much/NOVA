"""Phase 3B.4 static protections for websocket.py's runtime access.

The behaviour is proven against a real Home Assistant in
tests/integration/test_websocket_runtime.py. These checks keep the shape in
place on every unit run:

* websocket.py never reads hass.data, so no entry-runtime lookup goes
  through the compatibility bridge,
* nothing creates runtime_config with setdefault,
* the canonical resolver is only asked for the layers below runtime_config,
* every executor job that merges runtime_config receives a fresh snapshot,
  never the live dict,
* observer_enabled is saved only after the observer change and state update.

Focused run:
    python -m pytest tests/unit/test_websocket_runtime_static.py -q
"""
import ast
import pathlib

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
WS = COMP / "websocket.py"

# Calls that return a fresh copy of runtime_config.
SNAPSHOT_CALLS = {"_executor_runtime_config", "runtime_config_snapshot"}
# Calls or attributes that return the live dict.
LIVE_NAMES = {"_live_runtime_config", "lifecycle_runtime_config", "runtime_config"}


def _tree():
    return ast.parse(WS.read_text(encoding="utf-8"))


def _func(name):
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _call_name(node):
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def test_websocket_never_reads_hass_data():
    uses = [n.lineno for n in ast.walk(_tree())
            if isinstance(n, ast.Attribute) and n.attr == "data"
            and isinstance(n.value, ast.Name) and n.value.id == "hass"]
    assert uses == []


def test_no_setdefault_creates_runtime_config():
    src = WS.read_text(encoding="utf-8")
    assert 'setdefault("runtime_config"' not in src
    assert "setdefault('runtime_config'" not in src


def test_resolver_is_only_used_below_runtime_config():
    """runtime_get(hass, …) would read the bridge; websocket.py passes None
    and reads the live runtime itself."""
    calls = [n for n in ast.walk(_tree()) if _call_name(n) == "runtime_get"]
    assert calls
    for call in calls:
        first = call.args[0]
        assert isinstance(first, ast.Constant) and first.value is None, call.lineno


def _executor_calls(tree):
    return [n for n in ast.walk(tree) if _call_name(n) == "async_add_executor_job"]


def _is_live(arg) -> bool:
    if isinstance(arg, ast.Name) and arg.id in {"rc", "runtime_config"}:
        return True
    if isinstance(arg, ast.Attribute) and arg.attr in LIVE_NAMES:
        return True
    return _call_name(arg) in LIVE_NAMES


def test_no_live_runtime_dict_is_passed_to_an_executor():
    for call in _executor_calls(_tree()):
        for arg in call.args[1:]:
            assert not _is_live(arg), call.lineno


def test_every_runtime_merge_gets_a_snapshot():
    """Every effective_config_with_runtime job gets a snapshot call inline, or
    a local built from one in the same function (the observer candidate)."""
    tree = _tree()
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    found = {}
    for fn in funcs:
        snapshot_locals = {
            t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
            and _call_name(n.value) in SNAPSHOT_CALLS
            for t in n.targets if isinstance(t, ast.Name)}
        for call in _executor_calls(fn):
            job = call.args[0] if call.args else None
            if not (isinstance(job, ast.Attribute)
                    and job.attr == "effective_config_with_runtime"):
                continue
            rc_arg = call.args[2]
            ok = (_call_name(rc_arg) in SNAPSHOT_CALLS
                  or (isinstance(rc_arg, ast.Name) and rc_arg.id in snapshot_locals))
            assert ok, (fn.name, call.lineno)
            found[fn.name] = found.get(fn.name, 0) + 1
    assert found == {
        "ws_reload_appliances": 1,
        "ws_update_config": 1,
        "ws_list_models": 1,
        "ws_apply_ai_config": 1,
        "ws_get_credential_status": 1,
    }


def test_apply_ai_config_merges_runtime_and_writes_the_runtime():
    fn = _func("ws_apply_ai_config")
    names = {_call_name(n) for n in ast.walk(fn)}
    assert "get_runtime" in names
    src = ast.unparse(fn)
    assert "effective_config, entry" not in src          # no runtime-free view
    assert "runtime.runtime_config.update(persisted_updates)" in src
    # Ownership comes before the executor job, the tests and the save.
    lines = {name: min(n.lineno for n in ast.walk(fn) if _call_name(n) == name)
             for name in ("get_runtime", "async_add_executor_job", "test_connection")}
    assert lines["get_runtime"] < lines["async_add_executor_job"] < lines["test_connection"]


def test_update_config_resolves_runtime_before_any_write():
    fn = _func("ws_update_config")
    first_runtime = min(n.lineno for n in ast.walk(fn) if _call_name(n) == "get_runtime")
    writes = [n.lineno for n in ast.walk(fn)
              if isinstance(n, ast.Assign) and any(
                  isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                  and t.value.id == "rc" for t in n.targets)]
    persists = [n.lineno for n in ast.walk(fn)
                if _call_name(n) == "async_add_executor_job"
                and isinstance(n.args[0], ast.Attribute) and n.args[0].attr == "set"]
    assert len(writes) == 1 and len(persists) == 1
    assert first_runtime < writes[0] < persists[0]


def test_observer_change_comes_before_state_config_and_save():
    fn = _func("ws_update_config")

    def _lines(pred):
        return sorted(n.lineno for n in ast.walk(fn) if pred(n))

    (start,) = _lines(lambda n: _call_name(n) == "start")
    (stop,) = _lines(lambda n: _call_name(n) == "stop")
    state_on, state_off = _lines(lambda n: _call_name(n) == "set_observer_running")
    (write,) = _lines(lambda n: isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
        and t.value.id == "rc" for t in n.targets))
    (persist,) = _lines(lambda n: _call_name(n) == "async_add_executor_job"
                        and isinstance(n.args[0], ast.Attribute)
                        and n.args[0].attr == "set")
    # enable: start → state; disable: stop → state; then write, then save.
    assert start < state_on < stop < state_off < write < persist
