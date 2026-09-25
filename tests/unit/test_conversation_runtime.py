"""Phase 3B.3 conversation.py on NovaRuntime, the unit-level half.

The PHACC file (tests/integration/test_conversation_runtime.py) drives real
conversation turns. This file runs the real NovaAgent config and routing
methods, pulled out of conversation.py with ast, against fakes:

* the agent's client is runtime.client by identity, and construction never
  builds a provider,
* a missing runtime stops construction and every config or pairings read,
* the hass.data bridge is never touched: a missing, damaged or drifted bridge
  changes nothing,
* _opt/_rt_opt keep nova_config.runtime_get's precedence exactly
  (runtime_config -> config.json -> options -> data -> default), including
  None/"" runtime values and the self-hosted endpoint blank rule,
* in-place runtime_config changes show on the next call (no stale copy),
* one pairings parser serves both routing sites and keeps the old parsing
  result for dicts, JSON strings and malformed values,
* static checks on the call order inside a turn and on the reasoning
  fallback's config.

Focused run:
    python -m pytest tests/unit/test_conversation_runtime.py -q
"""
import __future__
import ast
import json
import logging
import pathlib
import types

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
SRC = (COMP / "conversation.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

METHODS = ("__init__", "_runtime_config", "_opt", "_rt_opt",
           "_satellite_pairings", "_model")


def _agent_class_node() -> ast.ClassDef:
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == "NovaAgent":
            return node
    raise AssertionError("NovaAgent class not found")


def _method(name: str) -> ast.FunctionDef:
    for node in _agent_class_node().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"NovaAgent.{name} not found")


# ── Fakes ───────────────────────────────────────────────────────────────────

class _NoBridgeHass:
    """A hass whose .data must never be read by the migrated code."""

    @property
    def data(self):
        raise AssertionError("conversation.py read hass.data")


class _Entry:
    def __init__(self, options=None, data=None, entry_id="e1"):
        self.options = options or {}
        self.data = data or {}
        self.entry_id = entry_id


@pytest.fixture
def rt(load):
    return load("runtime")


@pytest.fixture
def jc(load):
    m = load("nova_config")
    m._loaded = True
    m._cache = {}
    yield m
    m._cache = {}


@pytest.fixture
def const(load):
    return load("const")


@pytest.fixture
def providers_built():
    return []


@pytest.fixture
def Agent(rt, jc, const, providers_built):
    """A class carrying the real NovaAgent methods listed in METHODS."""
    def _create_provider(*a, **kw):
        providers_built.append((a, kw))
        raise AssertionError("conversation.py built a provider")

    ns = {
        "__name__": "jc._conversation_under_test",
        "__package__": "jc",
        "get_runtime": rt.get_runtime,
        "create_provider": _create_provider,
        "DeviceInfo": lambda **kw: kw,
        "DOMAIN": const.DOMAIN,
        "CONF_MODEL": const.CONF_MODEL,
        "DEFAULT_MODEL": const.DEFAULT_MODEL,
        "_LOGGER": logging.getLogger("test_conversation_runtime"),
        "HomeAssistant": object,
        "ConfigEntry": object,
    }
    module = ast.Module(body=[_method(n) for n in METHODS], type_ignores=[])
    code = compile(module, str(COMP / "conversation.py"), "exec",
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    exec(code, ns)
    return type("NovaAgentUnderTest", (), {n: ns[n] for n in METHODS})


def _runtime(rt, client=None, runtime_config=None):
    return rt.NovaRuntime(
        client=client if client is not None else types.SimpleNamespace(name="ollama"),
        llm_provider_name="ollama", sentinel=object(), reminder_watcher=object(),
        scheduler=object(), resources=object(), automation_contexts=object(),
        runtime_config={} if runtime_config is None else runtime_config,
    )


def _loaded_entry(rt, runtime_config=None, **kw):
    entry = _Entry(**kw)
    entry.runtime_data = _runtime(rt, runtime_config=runtime_config)
    return entry


def _bridge_hass(const, entry_id="e1", **bridge):
    """A hass with a (drifted) compatibility bridge."""
    return types.SimpleNamespace(data={const.DOMAIN: {entry_id: bridge}})


# ── Provider client ownership ───────────────────────────────────────────────

def test_client_is_runtime_client_by_identity(Agent, rt, providers_built):
    entry = _loaded_entry(rt)
    agent = Agent(_NoBridgeHass(), entry)
    assert agent._client is entry.runtime_data.client
    assert providers_built == []


def test_construction_never_builds_a_provider(Agent, rt, providers_built):
    entry = _loaded_entry(rt, runtime_config={"llm_provider": "groq"})
    Agent(_NoBridgeHass(), entry)
    assert providers_built == []
    assert "create_provider" not in SRC


def test_missing_runtime_prevents_construction(Agent, rt, providers_built):
    with pytest.raises(rt.NovaRuntimeUnavailable):
        Agent(_NoBridgeHass(), _Entry())
    assert providers_built == []


def test_missing_runtime_fails_before_anything_else_in_init():
    first = _method("__init__").body[0]
    assert ast.unparse(first) == "runtime = get_runtime(entry)"
    assigns = [ast.unparse(n) for n in ast.walk(_method("__init__"))
               if isinstance(n, ast.Assign) and "_client" in ast.unparse(n.targets[0])]
    assert assigns == ["self._client = runtime.client"]


@pytest.mark.parametrize("store", [{}, {"nova": {}}, {"nova": "broken"},
                                   {"nova": {"e1": "broken"}},
                                   {"nova": {"e1": {"client": None}}}])
def test_missing_or_damaged_bridge_does_not_affect_construction(
        Agent, rt, store, providers_built):
    entry = _loaded_entry(rt, runtime_config={"model": "rt-model"})
    agent = Agent(types.SimpleNamespace(data=store), entry)
    assert agent._client is entry.runtime_data.client
    assert agent._model() == "rt-model"
    assert providers_built == []


def test_drifted_bridge_client_and_config_are_ignored(Agent, rt, const):
    entry = _loaded_entry(rt, runtime_config={"model": "live"})
    stale_client = object()
    hass = _bridge_hass(const, client=stale_client,
                        runtime_config={"model": "stale",
                                        "satellite_pairings": {"s": "m"}})
    agent = Agent(hass, entry)
    assert agent._client is entry.runtime_data.client
    assert agent._client is not stale_client
    assert agent._opt("model") == "live"
    assert agent._satellite_pairings() is None


# ── Configuration precedence ────────────────────────────────────────────────

def test_runtime_config_has_highest_precedence(Agent, rt, jc):
    jc._cache = {"honorific": "json"}
    entry = _loaded_entry(rt, runtime_config={"honorific": "live"},
                          options={"honorific": "opt"}, data={"honorific": "data"})
    agent = Agent(_NoBridgeHass(), entry)
    assert agent._opt("honorific", "d") == "live"
    assert agent._rt_opt("honorific", "d") == "live"


def test_config_json_above_options_and_data(Agent, rt, jc):
    jc._cache = {"honorific": "json"}
    entry = _loaded_entry(rt, options={"honorific": "opt"}, data={"honorific": "data"})
    agent = Agent(_NoBridgeHass(), entry)
    assert agent._opt("honorific", "d") == "json"
    assert agent._rt_opt("honorific", "d") == "json"


def test_options_above_data(Agent, rt):
    entry = _loaded_entry(rt, options={"honorific": "opt"}, data={"honorific": "data"})
    agent = Agent(_NoBridgeHass(), entry)
    assert agent._opt("honorific", "d") == "opt"
    entry.options = {}
    assert agent._opt("honorific", "d") == "data"


def test_default_unchanged(Agent, rt, const):
    agent = Agent(_NoBridgeHass(), _loaded_entry(rt))
    assert agent._opt("honorific", "d") == "d"
    assert agent._opt("honorific") is None
    assert agent._model() == const.DEFAULT_MODEL


@pytest.mark.parametrize("blank", [None, ""])
def test_empty_runtime_values_fall_through(Agent, rt, jc, blank):
    jc._cache = {"honorific": "json"}
    entry = _loaded_entry(rt, runtime_config={"honorific": blank},
                          options={"honorific": "opt"})
    agent = Agent(_NoBridgeHass(), entry)
    assert agent._opt("honorific", "d") == "json"
    jc._cache = {}
    assert agent._opt("honorific", "d") == "opt"


@pytest.mark.parametrize("value", [0, False, [], {}])
def test_falsy_but_set_runtime_values_still_win(Agent, rt, jc, value):
    jc._cache = {"k": "json"}
    agent = Agent(_NoBridgeHass(), _loaded_entry(rt, runtime_config={"k": value}))
    assert agent._opt("k", "d") == value


@pytest.mark.parametrize("key", ["ollama_base_url", "custom_base_url"])
def test_self_hosted_endpoint_blank_semantics(Agent, rt, jc, key):
    entry = _loaded_entry(rt, options={key: "http://old:11434"})
    agent = Agent(_NoBridgeHass(), entry)
    # Migrated install: an explicit blank in config.json means "cleared".
    jc._cache = {"self_hosted_endpoints_migrated": True, key: ""}
    assert agent._opt(key, "d") == ""
    entry.runtime_data.runtime_config[key] = ""
    assert agent._opt(key, "d") == ""
    entry.runtime_data.runtime_config[key] = "http://live:11434"
    assert agent._opt(key, "d") == "http://live:11434"
    # Not migrated: a blank config.json value falls through to the entry.
    entry.runtime_data.runtime_config.clear()
    jc._cache = {key: ""}
    assert agent._opt(key, "d") == "http://old:11434"


PARITY_CASES = [
    # (runtime_config, config.json, options, data)
    ({}, {}, {}, {}),
    ({"k": "rt"}, {"k": "js"}, {"k": "op"}, {"k": "da"}),
    ({"k": None}, {"k": "js"}, {"k": "op"}, {"k": "da"}),
    ({"k": ""}, {}, {"k": "op"}, {"k": "da"}),
    ({"k": ""}, {"k": None}, {}, {"k": "da"}),
    ({}, {"k": ""}, {"k": None}, {"k": "da"}),
    ({"k": 0}, {"k": "js"}, {}, {}),
    ({"other": "x"}, {}, {}, {"k": False}),
    ({}, {"self_hosted_endpoints_migrated": True, "k": ""}, {"k": "op"}, {}),
    ({"k": [1]}, {"k": [2]}, {}, {}),
]


@pytest.mark.parametrize("rc,cfg,opts,data", PARITY_CASES)
@pytest.mark.parametrize("key", ["k", "ollama_base_url"])
def test_opt_matches_runtime_get_for_every_case(Agent, rt, jc, const,
                                                rc, cfg, opts, data, key):
    """The migrated read gives exactly what the old bridge-backed
    runtime_get call gave for the same runtime_config."""
    def _rekey(d):
        return {(key if k == "k" else k): v for k, v in d.items()}
    rc, cfg, opts, data = map(_rekey, (rc, cfg, opts, data))
    jc._cache = dict(cfg)
    entry = _loaded_entry(rt, runtime_config=dict(rc), options=opts, data=data)
    agent = Agent(_NoBridgeHass(), entry)
    old = jc.runtime_get(_bridge_hass(const, runtime_config=dict(rc)), entry, key, "D")
    assert agent._opt(key, "D") == old
    assert agent._rt_opt(key, "D") == old


def test_in_place_runtime_change_applies_to_next_call(Agent, rt, jc):
    jc._cache = {"model": "json-model"}
    entry = _loaded_entry(rt)
    agent = Agent(_NoBridgeHass(), entry)
    live = entry.runtime_data.runtime_config
    assert agent._opt("model") == "json-model"
    live["model"] = "panel-model"
    assert agent._opt("model") == "panel-model"
    assert agent._rt_opt("model") == "panel-model"
    live["model"] = "panel-model-2"
    assert agent._rt_opt("model") == "panel-model-2"
    del live["model"]
    assert agent._opt("model") == "json-model"


def test_runtime_config_is_the_runtime_dict_itself(Agent, rt):
    entry = _loaded_entry(rt)
    agent = Agent(_NoBridgeHass(), entry)
    assert agent._runtime_config() is entry.runtime_data.runtime_config


def test_lost_runtime_fails_every_read(Agent, rt):
    entry = _loaded_entry(rt, runtime_config={"model": "x"})
    agent = Agent(_NoBridgeHass(), entry)
    del entry.runtime_data
    for call in (lambda: agent._opt("model"), lambda: agent._rt_opt("model"),
                 agent._runtime_config, agent._satellite_pairings):
        with pytest.raises(rt.NovaRuntimeUnavailable):
            call()


# ── Satellite pairings ──────────────────────────────────────────────────────

def _old_parser(rc):
    """The parsing block both routing sites used before Phase 3B.3."""
    sat_pairings = None
    try:
        _raw = rc.get("satellite_pairings")
        if _raw:
            _parsed = json.loads(_raw) if isinstance(_raw, str) else _raw
            if isinstance(_parsed, dict) and _parsed:
                sat_pairings = _parsed
    except Exception:
        pass
    return sat_pairings


class _Weird:
    """Truthy, not a string, not a dict."""


PAIRING_VALUES = [
    {"dev1": "media_player.kitchen"},
    json.dumps({"dev1": "media_player.kitchen"}),
    '{"dev1": "media_player.kitchen", "dev2": "media_player.lounge"}',
    {}, "{}", "", None, "   ", "{bad json", "null", "[1, 2]", '"text"',
    "0", ["dev1"], ("a",), 5, True, _Weird(),
]


@pytest.mark.parametrize("value", PAIRING_VALUES)
def test_pairings_parser_matches_old_behaviour(Agent, rt, value):
    rc = {"satellite_pairings": value}
    agent = Agent(_NoBridgeHass(), _loaded_entry(rt, runtime_config=rc))
    assert agent._satellite_pairings() == _old_parser(rc)


def test_pairings_absent_is_none(Agent, rt):
    assert Agent(_NoBridgeHass(), _loaded_entry(rt))._satellite_pairings() is None


def test_pairings_dict_and_json_string(Agent, rt):
    entry = _loaded_entry(rt, runtime_config={"satellite_pairings": {"d": "m"}})
    agent = Agent(_NoBridgeHass(), entry)
    assert agent._satellite_pairings() == {"d": "m"}
    entry.runtime_data.runtime_config["satellite_pairings"] = '{"d": "m2"}'
    assert agent._satellite_pairings() == {"d": "m2"}


def test_pairings_read_live_every_call(Agent, rt):
    entry = _loaded_entry(rt)
    agent = Agent(_NoBridgeHass(), entry)
    live = entry.runtime_data.runtime_config
    assert agent._satellite_pairings() is None
    live["satellite_pairings"] = {"d": "a"}
    assert agent._satellite_pairings() == {"d": "a"}
    live["satellite_pairings"] = {"d": "b"}
    assert agent._satellite_pairings() == {"d": "b"}
    live["satellite_pairings"]["d"] = "c"
    assert agent._satellite_pairings() == {"d": "c"}


# ── Static checks on conversation.py ────────────────────────────────────────

def _is_hass_data(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "data"
            and ast.unparse(node.value) in ("hass", "self.hass"))


def test_conversation_has_no_hass_data_access():
    uses = [n.lineno for n in ast.walk(TREE) if _is_hass_data(n)]
    assert uses == []
    assert "hass.data" not in SRC
    assert '"runtime_config"' not in SRC and "'runtime_config'" not in SRC


def test_one_pairings_parser_used_by_both_routing_sites():
    literals = [n for n in ast.walk(TREE)
                if isinstance(n, ast.Constant) and n.value == "satellite_pairings"]
    assert len(literals) == 1
    owner = _method("_satellite_pairings")
    assert any(n is literals[0] for n in ast.walk(owner))
    calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "self._satellite_pairings"]
    assert len(calls) == 2
    speakers = _method("_speakers")
    impl = _method("_handle_message_impl")
    assert sum(1 for n in ast.walk(speakers) if n in calls) == 1
    assert sum(1 for n in ast.walk(impl) if n in calls) == 1


def test_opt_reads_runtime_then_resolver_without_bridge():
    src = ast.unparse(_method("_opt"))
    assert "rc = self._runtime_config()" in src
    assert "nova_config.runtime_get(None, self.entry, key, default)" in src
    assert ast.unparse(_method("_rt_opt").body[-1]) == "return self._opt(key, default)"
    assert ast.unparse(_method("_runtime_config").body[-1]) == \
        "return get_runtime(self.entry).runtime_config"


def test_reasoning_fallback_uses_effective_config_with_live_runtime():
    impl = _method("_handle_message_impl")
    jobs = [n for n in ast.walk(impl) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "self.hass.async_add_executor_job"
            and n.args and "effective_config" in ast.unparse(n.args[0])]
    assert len(jobs) == 1
    assert [ast.unparse(a) for a in jobs[0].args] == [
        "_jc.effective_config_with_runtime", "self.entry", "self._runtime_config()"]


def _first_line(fn, needle: str) -> int:
    lines = [n.lineno for n in ast.walk(fn)
             if isinstance(n, (ast.Name, ast.Attribute))
             and ast.unparse(n).endswith(needle)]
    assert lines, f"{needle} not found"
    return min(lines)


def test_turn_checks_runtime_before_any_work():
    impl = _method("_handle_message_impl")
    guard = [n.lineno for n in ast.walk(impl) if isinstance(n, ast.Call)
             and ast.unparse(n) == "get_runtime(self.entry)"]
    assert len(guard) == 1
    for later in ("_check_and_claim_dedup", "self._opt", "self._honorific",
                  "try_local", "run_agent", "self._satellite_pairings",
                  "reply_target", "self._speakers", "async_announce",
                  "save_message", "store_memory"):
        assert guard[0] < _first_line(impl, later), later
    # It sits directly in the method body, outside any try block.
    assert any(isinstance(s, ast.Expr) and ast.unparse(s) == "get_runtime(self.entry)"
               for s in impl.body)


def test_turn_errors_surface_through_the_existing_handler():
    handler = _method("_async_handle_message")
    src = ast.unparse(handler)
    assert "_LOGGER.exception('Nova conversation handler crashed')" in src
    assert "I ran into an internal error handling that request." in src
