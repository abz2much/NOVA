"""Phase 3B Final: every remaining runtime consumer reads NovaRuntime.

The hass.data[DOMAIN][entry_id] bridge is passive until Phase 3C. This file
proves with fakes that each migrated consumer

* reads the loaded entry's NovaRuntime (runtime_config or its objects),
* ignores a bridge that is missing, damaged or out of step with the runtime,
* sees a live runtime change on its next call,
* fails with NovaRuntimeUnavailable when a LOADED entry has lost its runtime,
* stays safe (lower-precedence defaults) while the entry is not loaded,

plus the runtime helpers behind them and the executor-boundary rules: a
worker thread gets a fresh snapshot taken on the event loop, never the live
dict. The real-Home-Assistant half is tests/integration/test_runtime_consumers.py
and tests/integration/test_proactive_audio_runtime.py.

Focused run:
    python -m pytest tests/unit/test_runtime_consumers.py -q
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types

import pytest

from conftest import _install_nova_runtime
from fakes import FakeHass

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
STATES = sys.modules["homeassistant.config_entries"].ConfigEntryState


@pytest.fixture
def rt(load):
    return load("runtime")


def _bare_entry(state):
    """A Nova entry with no runtime, in `state`."""
    return types.SimpleNamespace(entry_id="e1", state=state, options={}, data={})


def _with_entry(hass, entry):
    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [entry])
    return entry


def _bridge(hass, runtime_config):
    """Put a bridge dict with `runtime_config` where the old readers looked."""
    hass.data = {"nova": {"e1": {"runtime_config": runtime_config,
                                 "scheduler": object(),
                                 "automation_inventory": object()}}}


# ── One probe per migrated consumer ─────────────────────────────────────────
# Each case: runtime_config key, the value to set, a call(hass, entry, mods)
# returning the consumer's answer, the answer with the value, and the
# default answer without it.

class _Case:
    def __init__(self, name, key, value, call, with_value, default):
        self.name, self.key, self.value = name, key, value
        self.call, self.with_value, self.default = call, with_value, default

    def __repr__(self):
        return self.name


def _tts(hass, entry, load):
    return load("tts_helper").tts_use_ha_voice(hass)


def _movie(hass, entry, load):
    hass.states.set("media_player.speaker", "idle")
    hass.states.set("media_player.projector", "idle")
    return load("audio_routing").drop_display_targets(
        hass, ["media_player.speaker", "media_player.projector"], "unit")


def _excluded(hass, entry, load):
    return load("entity_filter").is_excluded(hass, "light.den")


def _alarm(hass, entry, load):
    return load("alarm_source")._configured(hass, {"security_alarm_entity": "alarm_control_panel.cfg"})


def _package(hass, entry, load):
    return load("package_monitor")._announcements_on(hass)


def _rich(hass, entry, load):
    return load("reasoning_loop")._rich_mode(hass)


def _owner(module_name, state_attr):
    """Consumers owned by the entry that started them (observer, appliance
    monitor, cognitive core): set their owner, then read."""
    def run(fn):
        def call(hass, entry, load):
            mod = load(module_name)
            state = getattr(mod, state_attr)
            old = (state.entry, getattr(state, "hass", None))
            state.entry = entry
            state.hass = hass
            try:
                return fn(mod)
            finally:
                state.entry, state.hass = old
        return call
    return run


CASES = [
    _Case("tts_helper", "tts_use_ha_voice", True, _tts, True, False),
    _Case("audio_routing", "movie_media_player", "media_player.projector", _movie,
          ["media_player.speaker"], ["media_player.speaker", "media_player.projector"]),
    _Case("entity_filter", "excluded_entities", ["light.den"], _excluded, True, False),
    _Case("alarm_source", "security_alarm_entity", "alarm_control_panel.panel", _alarm,
          "alarm_control_panel.panel", "alarm_control_panel.cfg"),
    _Case("package_monitor", "announcements_enabled", False, _package, False, True),
    _Case("reasoning_loop", "rich_reasoning", True, _rich, True, False),
    _Case("observer.cognition", "cognition_enabled", False,
          _owner("observer", "_STATE")(lambda m: m._cognition_enabled()), False, True),
    _Case("observer.announcements", "announcements_enabled", False,
          _owner("observer", "_STATE")(lambda m: m._is_announcements_enabled()), False, True),
    _Case("observer.speakers", "announcement_speakers", ["media_player.a"],
          _owner("observer", "_STATE")(lambda m: m._get_announcement_speakers()),
          ["media_player.a"], None),
    _Case("observer.rate_limit", "classifier_rate_limit", 7,
          _owner("observer", "_STATE")(lambda m: m._effective_rate_limit()), 7,
          30),
    _Case("appliance_monitor", "appliance_profile", [{"name": "washer"}],
          _owner("appliance_monitor", "_MON")(lambda m: m._live_runtime_config().get("appliance_profile")),
          [{"name": "washer"}], None),
    _Case("cognitive_core", "announcement_speakers", ["media_player.b"],
          _owner("cognitive_core", "_CORE")(lambda m: m._live_runtime_config().get("announcement_speakers")),
          ["media_player.b"], None),
]


@pytest.fixture(autouse=True)
def _no_real_config(load, monkeypatch, tmp_path):
    """Keep every fallback read of config.json in this test's tmp dir."""
    jc = load("nova_config")
    monkeypatch.setattr(jc, "CONFIG_PATH", tmp_path / "nova" / "config.json")
    monkeypatch.setattr(jc, "_cache", {})
    monkeypatch.setattr(jc, "_loaded", False)


@pytest.fixture(autouse=True)
def _observer_defaults(load, monkeypatch):
    """The observer's config fallback, so its defaults are known."""
    obs = load("observer")
    monkeypatch.setattr(obs, "GLOBAL_CLASSIFIER_RATE_LIMIT_PER_HOUR", 30)
    monkeypatch.setattr(obs._STATE, "config", {})


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_reads_the_runtime(load, case):
    hass = FakeHass()
    entry = _install_nova_runtime(hass, {case.key: case.value})
    assert case.call(hass, entry, load) == case.with_value


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_bridge_only_value_is_ignored(load, case):
    hass = FakeHass()
    entry = _install_nova_runtime(hass, {})
    _bridge(hass, {case.key: case.value})
    assert case.call(hass, entry, load) == case.default


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_drifted_bridge_is_ignored(load, case):
    hass = FakeHass()
    entry = _install_nova_runtime(hass, {case.key: case.value})
    _bridge(hass, {case.key: "drifted"})
    assert case.call(hass, entry, load) == case.with_value


@pytest.mark.parametrize("damage", [None, "junk", {"e1": "junk"}, {"e1": {"runtime_config": "junk"}}])
@pytest.mark.parametrize("case", CASES, ids=repr)
def test_damaged_or_missing_bridge_has_no_effect(load, case, damage):
    hass = FakeHass()
    entry = _install_nova_runtime(hass, {case.key: case.value})
    hass.data = {} if damage is None else {"nova": damage}
    assert case.call(hass, entry, load) == case.with_value


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_live_change_is_seen_on_the_next_call(load, case):
    hass = FakeHass()
    live: dict = {}
    entry = _install_nova_runtime(hass, live)
    assert case.call(hass, entry, load) == case.default
    live[case.key] = case.value          # a panel write, in place
    assert case.call(hass, entry, load) == case.with_value


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_loaded_entry_without_runtime_fails_visibly(load, rt, case):
    hass = FakeHass()
    entry = _with_entry(hass, _bare_entry(STATES.LOADED))
    _bridge(hass, {case.key: case.value})     # a bridge must not stand in
    with pytest.raises(rt.NovaRuntimeUnavailable):
        case.call(hass, entry, load)


@pytest.mark.parametrize("state", [STATES.SETUP_IN_PROGRESS, STATES.NOT_LOADED,
                                   STATES.SETUP_ERROR, STATES.UNLOAD_IN_PROGRESS])
@pytest.mark.parametrize("case", CASES, ids=repr)
def test_entry_that_is_not_loaded_uses_defaults(load, case, state):
    hass = FakeHass()
    entry = _with_entry(hass, _bare_entry(state))
    _bridge(hass, {case.key: case.value})
    assert case.call(hass, entry, load) == case.default


def test_no_nova_entry_uses_defaults(load):
    hass = FakeHass()
    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [])
    _bridge(hass, {"tts_use_ha_voice": True, "rich_reasoning": True})
    assert load("tts_helper").tts_use_ha_voice(hass) is False
    assert load("reasoning_loop")._rich_mode(hass) is False


# ── Runtime-object consumers ────────────────────────────────────────────────

def test_automation_inventory_comes_from_the_runtime(load, rt):
    ai = load("automation_inventory")
    inventory = ai.AutomationInventory(FakeHass())
    hass = FakeHass()
    _install_nova_runtime(hass, automation_inventory=inventory)
    hass.data = {"nova": {"e1": {"automation_inventory": ai.AutomationInventory(hass)}}}
    assert ai.get_inventory(hass) is inventory


def test_automation_inventory_ignores_the_bridge(load):
    ai = load("automation_inventory")
    hass = FakeHass()
    _install_nova_runtime(hass)                       # no inventory built
    hass.data = {"nova": {"e1": {"automation_inventory": ai.AutomationInventory(hass)}}}
    assert ai.get_inventory(hass) is None


def test_automation_inventory_with_lost_runtime_raises(load, rt):
    ai = load("automation_inventory")
    hass = FakeHass()
    _with_entry(hass, _bare_entry(STATES.LOADED))
    with pytest.raises(rt.NovaRuntimeUnavailable):
        ai.get_inventory(hass)


async def test_cognitive_core_takes_contexts_from_its_owner(load, rt, monkeypatch):
    core = load("cognitive_core")
    hass = FakeHass()
    contexts = object()
    entry = _install_nova_runtime(hass, automation_contexts=contexts)
    hass.data = {"nova": {"e1": {"automation_contexts": object()}}}
    # Stop right after ownership is resolved: nothing else runs.
    monkeypatch.setattr(core, "IgnoreManager", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(core._CORE, "running", False)
    with pytest.raises(RuntimeError, match="stop"):
        await core.start(hass, {}, entry=entry)
    try:
        assert core._CORE.automation_contexts is contexts
        assert core._CORE.entry is entry
    finally:
        await core.stop()
    assert core._CORE.entry is None and core._CORE.automation_contexts is None


async def test_cognitive_core_start_refuses_a_lost_runtime(load, rt, monkeypatch):
    core = load("cognitive_core")
    hass = FakeHass()
    entry = _with_entry(hass, _bare_entry(STATES.LOADED))
    monkeypatch.setattr(core._CORE, "running", False)
    before = core._CORE.hass
    with pytest.raises(rt.NovaRuntimeUnavailable):
        await core.start(hass, {}, entry=entry)
    assert core._CORE.running is False and core._CORE.hass is before


# ── Runtime helpers ─────────────────────────────────────────────────────────

def test_current_runtime(rt):
    hass = FakeHass()
    entry = _install_nova_runtime(hass)
    assert rt.current_runtime(entry) is entry.runtime_data
    for state in (STATES.SETUP_IN_PROGRESS, STATES.NOT_LOADED, STATES.SETUP_ERROR):
        assert rt.current_runtime(_bare_entry(state)) is None
    with pytest.raises(rt.NovaRuntimeUnavailable):
        rt.current_runtime(_bare_entry(STATES.LOADED))


def test_domain_runtime_config_is_the_live_dict(rt):
    hass = FakeHass()
    live = {"a": 1}
    entry = _install_nova_runtime(hass, live)
    assert rt.domain_runtime(hass) is entry.runtime_data
    assert rt.domain_runtime_config(hass) is live
    snap = rt.domain_runtime_config_snapshot(hass)
    assert snap == live and snap is not live


def test_domain_runtime_without_entries_or_registry(rt):
    assert rt.domain_runtime(types.SimpleNamespace(data={})) is None
    hass = FakeHass()
    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [])
    assert rt.domain_runtime(hass) is None
    assert rt.domain_runtime_config(hass) == {}


def test_domain_runtime_never_reads_the_bridge(rt):
    fn = _func("runtime.py", "domain_runtime")
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "data"]
    assert "async_entries(DOMAIN)" in ast.unparse(fn)


def test_mirror_to_bridge_writes_only_an_existing_bridge(rt):
    hass = types.SimpleNamespace(data={"nova": {"e1": {}}})
    entry = types.SimpleNamespace(entry_id="e1")
    value = [object()]
    rt.mirror_to_bridge(hass, entry, "k", value)
    assert hass.data["nova"]["e1"]["k"] is value
    for data in ({}, {"nova": "junk"}, {"nova": {}}, {"nova": {"e1": "junk"}}):
        before = repr(data)
        rt.mirror_to_bridge(types.SimpleNamespace(data=data), entry, "k", value)
        assert repr(data) == before          # no bridge: nothing written, no error


# ── Observer and appliance monitor ownership ────────────────────────────────

@pytest.fixture
def obs(load, monkeypatch):
    """observer with its subsystems stubbed: nothing real starts."""
    o = load("observer")
    started = {}
    for name in ("appliance_monitor", "proactive_briefing", "cognitive_core"):
        mod = load(name)

        async def _start(hass, config, entry=None, _n=name):
            started[_n] = entry

        async def _stop():
            return None
        monkeypatch.setattr(mod, "start", _start)
        monkeypatch.setattr(mod, "stop", _stop)
    monkeypatch.setattr(o, "create_tier_provider",
                        lambda cfg, tier: types.SimpleNamespace(name=tier, cfg=cfg))
    monkeypatch.setattr(o._STATE, "running", False)
    monkeypatch.setattr(o._STATE, "unsub", None)
    o._started = started
    yield o
    o._STATE.reset()


class _RecordingHass(FakeHass):
    """Runs executor jobs inline and records what crossed into them."""

    def __init__(self):
        super().__init__()
        self.executor_args: list = []

    async def async_add_executor_job(self, func, *args):
        self.executor_args.append(args)
        return func(*args)


def _crossed(hass, live) -> bool:
    return any(a is live for args in hass.executor_args for a in args)


async def test_observer_start_merges_a_snapshot_and_records_its_owner(obs):
    hass = _RecordingHass()
    live = {"reasoning_model": "live-model", "llm_base_url": "http://gpu", "other": 1}
    entry = _install_nova_runtime(hass, live)
    _bridge(hass, {"reasoning_model": "bridge-model"})

    await obs.start(hass, {"reasoning_model": "boot"}, entry=entry)

    assert obs._STATE.entry is entry
    assert obs._STATE.reasoning_provider.cfg["reasoning_model"] == "live-model"
    assert obs._STATE.config["llm_base_url"] == "http://gpu"
    assert "other" not in obs._STATE.config            # only AI keys merge
    assert not _crossed(hass, live)                     # never the live dict
    # The appliance monitor and cognitive core start for the same owner.
    assert obs._started["appliance_monitor"] is entry
    assert obs._started["cognitive_core"] is entry


async def test_observer_refresh_sees_newer_live_values(obs):
    hass = _RecordingHass()
    live = {"reasoning_model": "first"}
    entry = _install_nova_runtime(hass, live)
    await obs.start(hass, {}, entry=entry)
    live["reasoning_model"] = "second"
    await obs.refresh_tier_providers(hass, {"reasoning_model": "second"})
    assert obs._STATE.reasoning_provider.cfg["reasoning_model"] == "second"
    assert not _crossed(hass, live)


async def test_observer_start_refuses_a_lost_runtime_before_changing_state(obs, rt):
    hass = _RecordingHass()
    entry = _with_entry(hass, _bare_entry(STATES.LOADED))
    _bridge(hass, {"reasoning_model": "bridge"})
    with pytest.raises(rt.NovaRuntimeUnavailable):
        await obs.start(hass, {}, entry=entry)
    assert obs._STATE.running is False and obs._STATE.entry is None
    assert hass.executor_args == []


async def test_observer_notification_uses_the_owner_settings(obs, load, monkeypatch):
    hass = FakeHass()
    entry = _install_nova_runtime(hass, {"notify_service": "notify.live"})
    _bridge(hass, {"notify_service": "notify.bridge"})
    sent = []

    async def _send(hass_, config, payload, **kw):
        sent.append(config)
        return []
    monkeypatch.setattr(load("notify_targets"), "async_send_configured_notifications", _send)
    monkeypatch.setattr(obs._STATE, "hass", hass)
    monkeypatch.setattr(obs._STATE, "entry", entry)
    monkeypatch.setattr(obs._STATE, "config", {"notify_service": "notify.boot"})
    await obs._send_notification("hi", urgency="high")
    assert sent[0]["notify_service"] == "notify.live"


def test_observer_stop_forgets_its_owner(load):
    o = load("observer")
    o._STATE.entry = object()
    o._STATE.reset()
    assert o._STATE.entry is None


async def test_appliance_discovery_gets_an_exclusion_snapshot(load, monkeypatch):
    am = load("appliance_monitor")
    hass = _RecordingHass()
    live = {"excluded_entities": ["sensor.washer_power"]}
    entry = _install_nova_runtime(hass, live)
    hass.states.set("sensor.washer_power", "5", device_class="power")
    seen = {}

    def _discover(hass_, exclusions=None):
        seen["exclusions"] = exclusions
        # Reading the live config here would be a thread-safety bug; make
        # the entry look like it lost its runtime to prove nothing reads it.
        object.__delattr__(entry, "runtime_data")
        try:
            return {} if load("entity_filter").is_excluded(
                hass_, "sensor.washer_power", exclusions) else {"x": 1}
        finally:
            entry.runtime_data = rt_obj

    rt_obj = entry.runtime_data
    monkeypatch.setattr(am, "_discover_sensors", _discover)
    monkeypatch.setattr(am, "_discover_native_appliances", lambda hass_: {})
    monkeypatch.setattr(am, "_discover_whole_home_meter", lambda hass_: None)
    monkeypatch.setattr(am._MON, "running", False)
    try:
        await am.start(hass, {"appliance_profile": []}, entry=entry)
    finally:
        await am.stop()
    assert seen["exclusions"][0] == {"sensor.washer_power"}
    assert not _crossed(hass, live)


def test_discover_sensors_honours_the_snapshot_without_reading_runtime(load):
    am = load("appliance_monitor")
    hass = FakeHass()
    _with_entry(hass, _bare_entry(STATES.LOADED))     # a live read would raise
    hass.states.set("sensor.washer_power", "5", device_class="power",
                    friendly_name="Washer power")
    found = am._discover_sensors(hass, ({"sensor.washer_power"}, set(), set()))
    assert "sensor.washer_power" not in found


# ── Static executor-boundary checks ─────────────────────────────────────────

def _func(module: str, name: str) -> ast.AST:
    tree = ast.parse((COMP / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not in {module}")


def _executor_calls():
    for path in sorted(COMP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("async_add_executor_job", "run_in_executor")):
                yield str(path.relative_to(COMP)), node


LIVE_ACCESSORS = {"lifecycle_runtime_config", "domain_runtime_config",
                  "_live_runtime_config", "_live_runtime_config_for"}


def test_no_executor_job_is_handed_a_live_runtime_config():
    offenders = []
    for rel, call in _executor_calls():
        for arg in call.args:
            for n in ast.walk(arg):
                if isinstance(n, ast.Attribute) and n.attr == "runtime_config":
                    offenders.append((rel, call.lineno, "attribute .runtime_config"))
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in LIVE_ACCESSORS):
                    offenders.append((rel, call.lineno, n.func.id))
    assert offenders == []


def test_camera_client_jobs_take_settings_from_the_loop():
    jobs = [(rel, c) for rel, c in _executor_calls()
            if c.args and ast.unparse(c.args[0]).endswith("_make_client")]
    assert len(jobs) == 3            # vision + camera reasoning + packages
    for rel, call in jobs:
        last = call.args[-1]
        assert isinstance(last, ast.Call) and ast.unparse(last.func).endswith(
            "_client_settings"), (rel, call.lineno)


def test_appliance_discovery_job_takes_an_exclusion_snapshot():
    jobs = [c for rel, c in _executor_calls()
            if rel == "appliance_monitor.py" and c.args
            and ast.unparse(c.args[0]) == "_discover_sensors"]
    assert len(jobs) == 1
    assert ast.unparse(jobs[0].args[-1]) == "exclusion_snapshot(hass)"


@pytest.mark.parametrize("fn", ["start", "refresh_tier_providers"])
def test_observer_executor_config_is_merged_from_a_snapshot(fn):
    body = ast.unparse(_func("observer.py", fn))
    assert "runtime_config_snapshot(" in body
    assert "lifecycle_runtime_config" not in body


def test_make_client_reads_settings_not_config():
    body = ast.unparse(_func("camera.py", "_make_client"))
    assert "_cfg_opt" not in body and "_resolve_credential" not in body
    assert "settings = _client_settings(hass, provider)" in body   # loop-only fallback


def test_camera_client_settings(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    cam = load("camera")
    hass = FakeHass()
    live = {"ollama_base_url": "http://gpu:11434"}
    _install_nova_runtime(hass, live)
    first = cam._client_settings(hass, "ollama")
    assert first["ollama_base_url"] == "http://gpu:11434"
    assert first is not live
    live["ollama_base_url"] = "http://other:11434"
    assert cam._client_settings(hass, "ollama")["ollama_base_url"] == "http://other:11434"
    assert first["ollama_base_url"] == "http://gpu:11434"     # never re-read


def test_make_client_with_settings_never_reads_config(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    cam = load("camera")
    cam._PROVIDER_CACHE.clear()
    import importlib
    lp = importlib.import_module(cam.__name__.rsplit(".", 1)[0] + ".llm_provider")
    monkeypatch.setattr(lp, "create_provider",
                        lambda provider, key, model, base: types.SimpleNamespace(base=base))
    monkeypatch.setattr(cam, "_cfg_opt", lambda *a, **k: pytest.fail("read config in a thread"))
    client = cam._make_client(None, "ollama", "llava", "FB",
                              {"api_key": "", "ollama_base_url": "http://gpu:11434",
                               "custom_base_url": "", "llm_base_url": ""})
    assert client != "FB"
    cam._PROVIDER_CACHE.clear()


# ── Directive helper, sentinel and solar go through runtime_get ─────────────

def test_build_system_prompt_supplies_hass_to_the_directive():
    body = ast.unparse(_func("directive_helper.py", "build_system_prompt"))
    assert "resolve_directive(entry, hass)" in body
    resolve = ast.unparse(_func("directive_helper.py", "resolve_directive"))
    assert "runtime_get(hass, entry, CONF_DIRECTIVE_PRESET" in resolve
    assert "runtime_get(hass, entry, CONF_DIRECTIVE," in resolve


def test_solar_does_not_swallow_a_lost_runtime():
    body = ast.unparse(_func("solar.py", "_cost_today"))
    assert "except NovaRuntimeUnavailable:\n        raise" in body
