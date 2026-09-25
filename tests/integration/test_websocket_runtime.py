"""Phase 3B.4 WebSocket commands on NovaRuntime, against a real Home
Assistant (PHACC).

Drives the real setup and the real WebSocket commands to prove:

* the panel readers (_runtime_opt, _get_runtime_json, _get_runtime_str,
  _get_disabled_rules) read the live NovaRuntime.runtime_config, keep the
  runtime → config.json → options → data → default precedence and their
  parsing, ignore a missing, damaged or drifted bridge, and fail visibly for
  a loaded entry without a runtime,
* every executor job that merges runtime_config gets its own snapshot: an
  in-flight job keeps a coherent view and the next command sees new values,
* nova/update_config writes the runtime, never needs the bridge, and does
  nothing at all without a runtime,
* observer_enabled is transactional: the observer changes first, and only a
  successful change updates the observer state, runtime_config and
  config.json,
* nova/apply_ai_config checks ownership before any test or save, starts from
  live runtime values and updates the runtime only after a successful save,
* nova/list_models and nova/get_credential_status use live runtime
  endpoints and keep their payloads.

The observer, model discovery and connection tests are faked. No device is
touched: there are no lock, alarm or cover entities here.
"""
from contextlib import contextmanager

import pytest
from homeassistant.config_entries import ConfigEntryState

from .test_observer_runtime import no_panel_databases  # noqa: F401  (fixture)
from .test_runtime_config_snapshot import gate  # noqa: F401  (fixture)
from .test_runtime_data import (  # noqa: F401  (autouse fixtures)
    DOMAIN,
    _no_real_config,
    _restore_nova_config,
    _setup,
)

MARK = "phase_3b4_marker"   # not a real setting; merged like any other key


# ── Fixtures and helpers ────────────────────────────────────────────────────

@pytest.fixture
def events():
    return []


@pytest.fixture
def observer_fake(monkeypatch, events):
    """observer.start/stop that log what runtime_config, the observer state
    and config.json looked like at the moment they ran. Like the real
    observer, is_running() follows them. Set .fail_start or .fail_stop to
    make the call raise, or .noop_start or .noop_stop to make it return
    normally without changing is_running() (the real start() does this when
    its tier providers cannot be built)."""
    from custom_components.nova import cognitive_core, nova_config, observer

    ctl = type("Ctl", (), {"fail_start": False, "fail_stop": False,
                           "noop_start": False, "noop_stop": False,
                           "entry": None, "configs": []})()
    monkeypatch.setattr(observer._STATE, "running", False)

    def _seen():
        entry = ctl.entry
        rc = entry.runtime_data.runtime_config if entry else {}
        return (rc.get("observer_enabled"),
                entry.runtime_data.observer_running if entry else None,
                nova_config.get("observer_enabled"))

    async def _start(hass, config):
        events.append(("start",) + _seen())
        ctl.configs.append(config)
        if ctl.fail_start:
            raise RuntimeError("observer start failed")
        if not ctl.noop_start:
            observer._STATE.running = True

    async def _stop():
        events.append(("stop",) + _seen())
        if ctl.fail_stop:
            raise RuntimeError("observer stop failed")
        await cognitive_core.stop()
        if not ctl.noop_stop:
            observer._STATE.running = False

    monkeypatch.setattr(observer, "start", _start)
    monkeypatch.setattr(observer, "stop", _stop)
    return ctl


@pytest.fixture
def persist_spy(monkeypatch, events):
    """Wrap nova_config.set. .result forces its return value (None = real);
    .raise_ makes it raise instead."""
    from custom_components.nova import nova_config
    real = nova_config.set
    spy = type("Spy", (), {"calls": [], "result": None, "raise_": False,
                           "entry": None})()

    def _set(key, value):
        entry = spy.entry
        spy.calls.append((key, value))
        events.append(("persist", key, value,
                       entry.runtime_data.runtime_config.get(key) if entry else None,
                       entry.runtime_data.observer_running if entry else None))
        if spy.raise_:
            raise OSError("disk full")
        ok = real(key, value)
        return spy.result if spy.result is not None else ok

    monkeypatch.setattr(nova_config, "set", _set)
    return spy


@pytest.fixture
def side_effects(monkeypatch):
    """Record every side effect nova/update_config can apply."""
    from custom_components.nova import cognitive_core, observer, sleep_detection, websocket
    seen: list[tuple] = []

    monkeypatch.setattr(websocket, "invalidate_model_cache",
                        lambda provider=None: seen.append(("invalidate", provider)))
    monkeypatch.setattr(sleep_detection, "set_override",
                        lambda value, quiet_end: seen.append(("sleep", value, quiet_end)))

    async def _apply(key, value):
        seen.append(("lockdown", key, value))

    async def _refresh(hass, updates):
        seen.append(("refresh", updates))

    monkeypatch.setattr(cognitive_core, "apply_runtime_config", _apply)
    monkeypatch.setattr(observer, "refresh_tier_providers", _refresh)
    monkeypatch.setattr(observer, "is_running", lambda: True)
    return seen


@contextmanager
def _runtime_missing(entry):
    """Simulate a loaded entry that lost its runtime, then put it back."""
    runtime = entry.runtime_data
    object.__delattr__(entry, "runtime_data")
    try:
        yield runtime
    finally:
        entry.runtime_data = runtime


@contextmanager
def _bridge(hass, entry, damage):
    """Break the compatibility bridge one way, then restore it."""
    store = hass.data[DOMAIN]
    saved = store[entry.entry_id]
    if damage == "missing":
        del store[entry.entry_id]
    elif damage == "none":
        store[entry.entry_id] = None
    elif damage == "no_runtime_config":
        store[entry.entry_id] = {k: v for k, v in saved.items() if k != "runtime_config"}
    elif damage == "drifted":
        store[entry.entry_id] = {**saved, "runtime_config": {
            "chimney_side": "bridge", "floor_plan_elements": {"bridge": 1},
            "disabled_sentinel_rules": ["bridge"], "ollama_base_url": "http://bridge:11434",
            "custom_base_url": "http://bridge:8000/v1", MARK: "bridge"}}
    elif damage == "no_bucket":
        hass.data.pop(DOMAIN)
    try:
        yield
    finally:
        hass.data.setdefault(DOMAIN, store)[entry.entry_id] = saved


DAMAGE = ["missing", "none", "no_runtime_config", "drifted", "no_bucket"]


async def _ws(hass, hass_ws_client, payload) -> dict:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(payload)
    return await client.receive_json()


async def _update(hass, hass_ws_client, key, value) -> dict:
    return await _ws(hass, hass_ws_client,
                     {"type": "nova/update_config", "key": key, "value": value})


def _assert_observer(hass, entry, running: bool):
    assert entry.runtime_data.observer_running is running
    assert hass.data[DOMAIN][entry.entry_id]["observer_running"] is running


# ── Runtime readers ─────────────────────────────────────────────────────────

async def test_runtime_opt_reads_live_runtime_first(hass):
    from custom_components.nova import nova_config
    from custom_components.nova.websocket import _runtime_opt
    entry = await _setup(hass, garage_bays=5)
    rc = entry.runtime_data.runtime_config
    rc.pop("garage_bays", None)      # setup seeds it; read the lower layers

    assert _runtime_opt(hass, entry, "garage_bays", 3) == 5          # options
    nova_config.set("garage_bays", 4)
    assert _runtime_opt(hass, entry, "garage_bays", 3) == 4          # config.json
    rc["garage_bays"] = 2
    assert _runtime_opt(hass, entry, "garage_bays", 3) == 2          # runtime
    # None and "" fall through; False and 0 are real values.
    for blank in (None, ""):
        rc["garage_bays"] = blank
        assert _runtime_opt(hass, entry, "garage_bays", 3) == 4
    rc["garage_bays"] = 0
    assert _runtime_opt(hass, entry, "garage_bays", 3) == 0
    rc["has_basement"] = False
    assert _runtime_opt(hass, entry, "has_basement", True) is False
    assert _runtime_opt(hass, entry, "not_set_anywhere", "dflt") == "dflt"


async def test_runtime_opt_keeps_entry_data_below_options(hass):
    from custom_components.nova.websocket import _runtime_opt
    entry = await _setup(hass)
    model = entry.data["model"]
    assert _runtime_opt(hass, entry, "model", "dflt") == model       # entry.data
    entry.runtime_data.runtime_config["model"] = "live-model"
    assert _runtime_opt(hass, entry, "model", "dflt") == "live-model"


async def test_runtime_json_parsing(hass):
    from custom_components.nova import nova_config
    from custom_components.nova.websocket import _get_runtime_json
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config

    assert _get_runtime_json(hass, entry, "floor_plan_rooms", {"d": 1}) == {"d": 1}
    nova_config.set("floor_plan_rooms", '{"json": 1}')
    assert _get_runtime_json(hass, entry, "floor_plan_rooms", {}) == {"json": 1}
    live = {"live": [1, 2]}
    rc["floor_plan_rooms"] = live
    assert _get_runtime_json(hass, entry, "floor_plan_rooms", {}) is live
    rc["floor_plan_rooms"] = [1, 2]
    assert _get_runtime_json(hass, entry, "floor_plan_rooms", {}) == [1, 2]
    rc["floor_plan_rooms"] = '{"str": true}'
    assert _get_runtime_json(hass, entry, "floor_plan_rooms", {}) == {"str": True}
    rc["floor_plan_rooms"] = "not json"          # falls through to config.json
    assert _get_runtime_json(hass, entry, "floor_plan_rooms", {}) == {"json": 1}
    rc["floor_plan_rooms"] = None
    assert _get_runtime_json(hass, entry, "floor_plan_rooms", {}) == {"json": 1}


async def test_runtime_str_parsing(hass):
    from custom_components.nova import nova_config
    from custom_components.nova.websocket import _get_runtime_str
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config

    assert _get_runtime_str(hass, entry, "chimney_side", "right") == "right"
    nova_config.set("chimney_side", "json")
    assert _get_runtime_str(hass, entry, "chimney_side", "right") == "json"
    for raw, expected in (("left", "left"), ("", ""), (0, "0"), (False, "False")):
        rc["chimney_side"] = raw
        assert _get_runtime_str(hass, entry, "chimney_side", "right") == expected
    rc["chimney_side"] = None
    assert _get_runtime_str(hass, entry, "chimney_side", "right") == "json"


async def test_disabled_rules_read_runtime(hass):
    from custom_components.nova.websocket import _get_disabled_rules
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config

    assert _get_disabled_rules(hass, entry) == []
    rc["disabled_sentinel_rules"] = ["co_alarm"]
    assert _get_disabled_rules(hass, entry) == ["co_alarm"]
    rc["disabled_sentinel_rules"] = '["smoke", "leak"]'
    assert _get_disabled_rules(hass, entry) == ["smoke", "leak"]
    rc["disabled_sentinel_rules"] = "garbage"
    assert _get_disabled_rules(hass, entry) == []
    assert _get_disabled_rules(hass, None) == []


@pytest.mark.parametrize("damage", DAMAGE)
async def test_readers_ignore_the_bridge(hass, damage):
    from custom_components.nova.websocket import (
        _get_disabled_rules, _get_runtime_json, _get_runtime_str, _runtime_opt)
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc.update({"chimney_side": "left", "floor_plan_elements": {"live": 1},
               "disabled_sentinel_rules": ["live"]})
    with _bridge(hass, entry, damage):
        assert _runtime_opt(hass, entry, "chimney_side", "right") == "left"
        assert _get_runtime_str(hass, entry, "chimney_side", "right") == "left"
        assert _get_runtime_json(hass, entry, "floor_plan_elements", {}) == {"live": 1}
        assert _get_disabled_rules(hass, entry) == ["live"]


async def test_panel_request_sees_in_place_runtime_changes(
    hass, hass_ws_client, no_panel_databases,
):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config

    async def _config():
        resp = await _ws(hass, hass_ws_client, {"type": "nova/get_panel_data"})
        assert resp["success"], resp
        return resp["result"]["config"]

    first = await _config()
    assert first["chimney_side"] == "right"
    assert first["disabled_sentinel_rules"] == []

    rc.update({"chimney_side": "left", "disabled_sentinel_rules": ["co_alarm"],
               "floor_plan_elements": {"door": 1}})
    with _bridge(hass, entry, "drifted"):
        second = await _config()
    assert second["chimney_side"] == "left"
    assert second["disabled_sentinel_rules"] == ["co_alarm"]
    assert second["floor_plan_elements"] == {"door": 1}
    assert set(second) == set(first)             # shape unchanged


async def test_loaded_entry_without_runtime_fails_visibly(hass):
    from custom_components.nova.runtime import NovaRuntimeUnavailable
    from custom_components.nova.websocket import (
        _get_disabled_rules, _get_runtime_json, _get_runtime_str, _runtime_opt)
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["chimney_side"] = "left"
    with _runtime_missing(entry):
        for read in (lambda: _runtime_opt(hass, entry, "chimney_side", "right"),
                     lambda: _get_runtime_str(hass, entry, "chimney_side", "right"),
                     lambda: _get_runtime_json(hass, entry, "floor_plan_rooms", {}),
                     lambda: _get_disabled_rules(hass, entry)):
            with pytest.raises(NovaRuntimeUnavailable):
                read()


async def test_unloaded_or_missing_entry_keeps_defaults(hass):
    from custom_components.nova.websocket import (
        _get_disabled_rules, _get_runtime_json, _get_runtime_str, _runtime_opt)
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    for e in (entry, None):
        assert _runtime_opt(hass, e, "chimney_side", "right") == "right"
        assert _get_runtime_str(hass, e, "chimney_side", "right") == "right"
        assert _get_runtime_json(hass, e, "floor_plan_rooms", {"d": 1}) == {"d": 1}
        assert _get_disabled_rules(hass, e) == []


# ── Executor snapshots ──────────────────────────────────────────────────────

@pytest.fixture
def executor_fakes(monkeypatch, observer_fake):
    """Keep the executor paths off the network and off real subsystems."""
    from custom_components.nova import appliance_monitor, websocket
    seen: dict[str, list] = {"appliances": [], "models": []}

    async def _appliances(hass, cfg):
        seen["appliances"].append(cfg)

    async def _models(hass, provider, config, cache_key):
        seen["models"].append(config)
        return ["m1"], False, {}

    monkeypatch.setattr(appliance_monitor, "start", _appliances)
    monkeypatch.setattr(websocket, "_fetch_models_deduped", _models)
    websocket.invalidate_model_cache()
    yield seen
    websocket.invalidate_model_cache()


PATHS = {
    "reload_appliances": {"type": "nova/reload_appliances"},
    "observer_enable": {"type": "nova/update_config", "key": "observer_enabled",
                        "value": True},
    "list_models": {"type": "nova/list_models", "provider": "ollama", "refresh": True},
    "credential_status": {"type": "nova/get_credential_status"},
}


@pytest.mark.parametrize("path", list(PATHS))
async def test_executor_job_gets_a_coherent_snapshot(
    hass, hass_ws_client, gate, executor_fakes, observer_fake, path,
):
    entry = await _setup(hass)
    observer_fake.entry = entry
    live = entry.runtime_data.runtime_config
    live[MARK] = "first"
    expected = dict(live)
    if path == "observer_enable":
        expected["observer_enabled"] = True   # the operation's candidate

    client = await hass_ws_client(hass)
    gate.armed = True
    await client.send_json_auto_id(dict(PATHS[path]))
    assert await hass.async_add_executor_job(gate.started.wait, 10)
    given, when_given = gate.seen[-1]
    assert given is not live                      # never the live dict
    assert when_given == expected                 # equal when taken
    live[MARK] = "second"                         # panel write, on the loop
    gate.release.set()
    resp = await client.receive_json()
    assert resp["success"], resp
    assert gate.merged[-1][MARK] == "first"       # in-flight job stayed coherent
    assert given[MARK] == "first"

    # The next command takes a new snapshot and sees the new value. (The
    # observer is already on, so another runtime_config executor path stands
    # in for a second toggle.)
    if path == "observer_enable":
        next_payload = dict(PATHS["reload_appliances"])
    else:
        next_payload = dict(PATHS[path])
    resp = await _ws(hass, hass_ws_client, next_payload)
    assert resp["success"], resp
    given, when_given = gate.seen[-1]
    assert given is not live and when_given[MARK] == "second"


async def test_observer_start_gets_the_merged_candidate(
    hass, hass_ws_client, observer_fake, gate,
):
    entry = await _setup(hass)
    observer_fake.entry = entry
    entry.runtime_data.runtime_config[MARK] = "live"
    resp = await _update(hass, hass_ws_client, "observer_enabled", True)
    assert resp["success"], resp
    config = observer_fake.configs[-1]
    assert config[MARK] == "live" and config["observer_enabled"] is True


# ── nova/update_config: generic keys ────────────────────────────────────────

@pytest.mark.parametrize("damage", [None] + DAMAGE)
async def test_update_config_writes_runtime_without_the_bridge(
    hass, hass_ws_client, damage,
):
    from custom_components.nova import nova_config
    entry = await _setup(hass)
    runtime = entry.runtime_data
    if damage is None:
        resp = await _update(hass, hass_ws_client, "chimney_side", "left")
        assert hass.data[DOMAIN][entry.entry_id]["runtime_config"] is runtime.runtime_config
    else:
        with _bridge(hass, entry, damage):
            before = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            before = dict(before) if isinstance(before, dict) else before
            resp = await _update(hass, hass_ws_client, "chimney_side", "left")
            after = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            # The bridge is neither recreated nor repaired.
            assert (dict(after) if isinstance(after, dict) else after) == before
    assert resp["success"], resp
    assert resp["result"] == {"key": "chimney_side", "value": "left", "persisted": True}
    assert runtime.runtime_config["chimney_side"] == "left"
    assert nova_config.get("chimney_side") == "left"


MISSING_RUNTIME_CASES = [
    ("chimney_side", "left"),
    ("custom_base_url", "http://new:8000/v1"),
    ("llm_base_url", "http://new:11434"),
    ("sleep_override", "asleep"),
    ("security_alarm_entity", "alarm_control_panel.home"),
    ("lockdown_auto_on_arm", True),
    ("classifier_model", "tiny"),
    ("observer_enabled", True),
    ("observer_enabled", False),
]


@pytest.mark.parametrize(("key", "value"), MISSING_RUNTIME_CASES)
async def test_update_config_without_runtime_changes_nothing(
    hass, hass_ws_client, observer_fake, persist_spy, side_effects, events, key, value,
):
    from custom_components.nova import nova_config
    entry = await _setup(hass, observer_enabled=(key == "observer_enabled" and not value))
    runtime_rc_before = dict(entry.runtime_data.runtime_config)
    running_before = entry.runtime_data.observer_running
    json_before = nova_config.get(key)
    events.clear()
    side_effects.clear()
    persist_spy.calls.clear()

    with _runtime_missing(entry) as runtime:
        resp = await _update(hass, hass_ws_client, key, value)
        assert resp["success"] is False
        assert resp["error"]["code"] == "update_failed"
        assert runtime.runtime_config == runtime_rc_before   # memory unchanged
        assert persist_spy.calls == []                       # nothing saved
        assert nova_config.get(key) == json_before
        assert side_effects == []                            # no side effect
        assert events == []                                  # observer untouched
        assert runtime.observer_running is running_before
        assert hass.data[DOMAIN][entry.entry_id]["observer_running"] is running_before


@pytest.mark.parametrize("raise_", [False, True])
async def test_update_config_persist_failure_is_session_only(
    hass, hass_ws_client, persist_spy, raise_, caplog,
):
    entry = await _setup(hass)
    persist_spy.result = False
    persist_spy.raise_ = raise_
    resp = await _update(hass, hass_ws_client, "chimney_side", "left")
    assert resp["success"], resp
    assert resp["result"] == {"key": "chimney_side", "value": "left", "persisted": False}
    assert entry.runtime_data.runtime_config["chimney_side"] == "left"
    assert "will revert on restart" in caplog.text


@pytest.mark.parametrize(("key", "expected"), [
    ("llm_base_url", [("invalidate", "custom"), ("invalidate", "ollama")]),
    ("ollama_base_url", [("invalidate", "ollama")]),
    ("custom_base_url", [("invalidate", "custom")]),
    ("chimney_side", []),
])
async def test_update_config_model_cache_invalidation_unchanged(
    hass, hass_ws_client, side_effects, key, expected,
):
    await _setup(hass)
    side_effects.clear()
    resp = await _update(hass, hass_ws_client, key, "http://new:8000/v1")
    assert resp["success"], resp
    assert [e for e in side_effects if e[0] == "invalidate"] == expected


async def test_update_config_sleep_lockdown_and_provider_effects_unchanged(
    hass, hass_ws_client, side_effects,
):
    from custom_components.nova import nova_config
    entry = await _setup(hass)
    side_effects.clear()
    for key, value in (("sleep_override", "asleep"),
                       ("security_alarm_entity", "alarm_control_panel.home"),
                       ("lockdown_auto_on_arm", True),
                       ("classifier_provider", "ollama"),
                       ("reasoning_model", "big")):
        resp = await _update(hass, hass_ws_client, key, value)
        assert resp["success"], resp
        assert entry.runtime_data.runtime_config[key] == value
    quiet_end = nova_config.get("observer_quiet_end", "07:00")
    assert side_effects == [
        ("sleep", "asleep", quiet_end),
        ("lockdown", "security_alarm_entity", "alarm_control_panel.home"),
        ("lockdown", "lockdown_auto_on_arm", True),
        ("refresh", {"classifier_provider": "ollama"}),
        ("refresh", {"reasoning_model": "big"}),
    ]


# ── nova/update_config: observer_enabled transaction ────────────────────────

async def test_enable_starts_first_then_updates_state_config_and_saves(
    hass, hass_ws_client, observer_fake, persist_spy, events,
):
    entry = await _setup(hass)
    observer_fake.entry = persist_spy.entry = entry
    events.clear()
    resp = await _update(hass, hass_ws_client, "observer_enabled", True)
    assert resp["success"], resp
    assert resp["result"] == {"key": "observer_enabled", "value": True, "persisted": True}
    assert events == [
        # start ran with nothing changed yet: not in runtime_config, not
        # running, not saved,
        ("start", None, False, None),
        # and the save ran only after runtime_config and the state changed.
        ("persist", "observer_enabled", True, True, True),
    ]
    _assert_observer(hass, entry, True)
    assert entry.runtime_data.runtime_config["observer_enabled"] is True


async def test_disable_stops_first_then_updates_state_config_and_saves(
    hass, hass_ws_client, observer_fake, persist_spy, events,
):
    from custom_components.nova import nova_config
    entry = await _setup(hass, observer_enabled=True)
    observer_fake.entry = persist_spy.entry = entry
    _assert_observer(hass, entry, True)
    json_before = nova_config.get("observer_enabled")
    rc_before = entry.runtime_data.runtime_config.get("observer_enabled")
    events.clear()
    resp = await _update(hass, hass_ws_client, "observer_enabled", False)
    assert resp["success"], resp
    assert resp["result"] == {"key": "observer_enabled", "value": False, "persisted": True}
    assert events == [
        # stop ran with runtime_config, the state and config.json unchanged,
        ("stop", rc_before, True, json_before),
        ("persist", "observer_enabled", False, False, False),
    ]
    _assert_observer(hass, entry, False)
    assert entry.runtime_data.runtime_config["observer_enabled"] is False


@pytest.mark.parametrize("enable", [True, False])
async def test_failed_transition_changes_nothing(
    hass, hass_ws_client, observer_fake, persist_spy, events, enable,
):
    from custom_components.nova import nova_config
    entry = await _setup(hass, observer_enabled=not enable)
    observer_fake.entry = persist_spy.entry = entry
    observer_fake.fail_start = enable
    observer_fake.fail_stop = not enable
    rc_before = dict(entry.runtime_data.runtime_config)
    bridge_rc = hass.data[DOMAIN][entry.entry_id]["runtime_config"]
    json_before = nova_config.get("observer_enabled")
    events.clear()

    resp = await _update(hass, hass_ws_client, "observer_enabled", enable)
    assert resp["success"] is False
    assert resp["error"]["code"] == "update_failed"
    assert [e[0] for e in events] == ["start" if enable else "stop"]   # one attempt
    assert persist_spy.calls == []
    assert nova_config.get("observer_enabled") == json_before
    assert entry.runtime_data.runtime_config == rc_before
    assert bridge_rc is entry.runtime_data.runtime_config
    _assert_observer(hass, entry, not enable)


@pytest.mark.parametrize("enable", [True, False])
async def test_save_failure_after_transition_keeps_session_state(
    hass, hass_ws_client, observer_fake, persist_spy, events, enable, caplog,
):
    entry = await _setup(hass, observer_enabled=not enable)
    observer_fake.entry = persist_spy.entry = entry
    persist_spy.result = False
    events.clear()
    resp = await _update(hass, hass_ws_client, "observer_enabled", enable)
    assert resp["success"], resp
    assert resp["result"] == {"key": "observer_enabled", "value": enable, "persisted": False}
    assert [e[0] for e in events] == ["start" if enable else "stop", "persist"]  # no rollback
    _assert_observer(hass, entry, enable)
    assert entry.runtime_data.runtime_config["observer_enabled"] is enable
    assert "will revert on restart" in caplog.text


@pytest.fixture
def state_spy(monkeypatch):
    """Record set_observer_running calls (it is imported at call time)."""
    from custom_components.nova import runtime
    real = runtime.set_observer_running
    calls: list[bool] = []

    def _spy(hass, entry, running):
        calls.append(running)
        return real(hass, entry, running)

    monkeypatch.setattr(runtime, "set_observer_running", _spy)
    return calls


@pytest.mark.parametrize("enable", [True, False])
async def test_unconfirmed_transition_changes_nothing(
    hass, hass_ws_client, observer_fake, persist_spy, state_spy, events, enable,
):
    """start() or stop() returns normally but is_running() did not change."""
    from custom_components.nova import nova_config, observer
    entry = await _setup(hass, observer_enabled=not enable)
    observer_fake.entry = persist_spy.entry = entry
    assert observer.is_running() is (not enable)
    observer_fake.noop_start = enable
    observer_fake.noop_stop = not enable
    rc_before = dict(entry.runtime_data.runtime_config)
    json_before = nova_config.get("observer_enabled")
    events.clear()
    state_spy.clear()

    resp = await _update(hass, hass_ws_client, "observer_enabled", enable)
    assert resp["success"] is False
    assert resp["error"]["code"] == "update_failed"
    assert [e[0] for e in events] == ["start" if enable else "stop"]
    assert observer.is_running() is (not enable)
    assert state_spy == []                                   # never recorded
    assert persist_spy.calls == []
    assert nova_config.get("observer_enabled") == json_before
    assert entry.runtime_data.runtime_config == rc_before
    _assert_observer(hass, entry, not enable)


async def test_enable_when_already_running_does_not_restart(
    hass, hass_ws_client, observer_fake, persist_spy, events,
):
    from custom_components.nova import nova_config, observer
    entry = await _setup(hass)
    observer_fake.entry = persist_spy.entry = entry
    observer._STATE.running = True       # running, e.g. started elsewhere
    events.clear()
    resp = await _update(hass, hass_ws_client, "observer_enabled", True)
    assert resp["success"], resp
    assert resp["result"] == {"key": "observer_enabled", "value": True, "persisted": True}
    assert [e[0] for e in events] == ["persist"]             # no start
    _assert_observer(hass, entry, True)
    assert entry.runtime_data.runtime_config["observer_enabled"] is True
    assert nova_config.get("observer_enabled") is True


async def test_disable_when_already_stopped_does_not_stop_again(
    hass, hass_ws_client, observer_fake, persist_spy, events,
):
    from custom_components.nova import nova_config, observer
    entry = await _setup(hass, observer_enabled=True)
    observer_fake.entry = persist_spy.entry = entry
    observer._STATE.running = False      # stopped, e.g. by the service
    events.clear()
    resp = await _update(hass, hass_ws_client, "observer_enabled", False)
    assert resp["success"], resp
    assert resp["result"] == {"key": "observer_enabled", "value": False, "persisted": True}
    assert [e[0] for e in events] == ["persist"]             # no stop
    _assert_observer(hass, entry, False)
    assert entry.runtime_data.runtime_config["observer_enabled"] is False
    assert nova_config.get("observer_enabled") is False


# ── nova/apply_ai_config ────────────────────────────────────────────────────

def _ollama_roles(model="llama3"):
    return {
        "llm_provider": "ollama", "model": model,
        "classifier_provider": "ollama", "classifier_model": model,
        "reasoning_provider": "ollama", "reasoning_model": model,
        "vision_provider": "ollama", "vision_model": model,
        "camera_reasoning_provider": "ollama", "camera_reasoning_model": model,
    }


@pytest.fixture
def ai_fakes(monkeypatch, hass):
    """Fake connection tests, the atomic save, cache invalidation and the
    scheduled reload, recording each call."""
    from custom_components.nova import llm_provider, nova_config, websocket
    seen = type("Seen", (), {"tests": [], "saves": [], "invalidations": [],
                             "reloads": [], "test_result": None,
                             "save_result": True})()
    real_save = nova_config.set_many_atomic

    async def _test(hass_, provider, key, model, endpoint):
        seen.tests.append((provider, model, endpoint))
        return seen.test_result

    def _save(updates):
        seen.saves.append(dict(updates))
        return real_save(updates) if seen.save_result else False

    async def _reload(entry_id):
        seen.reloads.append(entry_id)
        return True

    monkeypatch.setattr(llm_provider, "test_connection", _test)
    monkeypatch.setattr(nova_config, "set_many_atomic", _save)
    monkeypatch.setattr(websocket, "invalidate_model_cache",
                        lambda provider=None: seen.invalidations.append(provider))
    monkeypatch.setattr(hass.config_entries, "async_reload", _reload)
    return seen


async def _apply(hass, hass_ws_client, updates) -> dict:
    resp = await _ws(hass, hass_ws_client,
                     {"type": "nova/apply_ai_config", "updates": updates})
    assert resp["success"], resp
    await hass.async_block_till_done()
    return resp["result"]


async def test_apply_without_runtime_tests_and_saves_nothing(
    hass, hass_ws_client, ai_fakes,
):
    entry = await _setup(hass)
    with _runtime_missing(entry) as runtime:
        before = dict(runtime.runtime_config)
        result = await _apply(hass, hass_ws_client,
                              {**_ollama_roles(), "ollama_base_url": "http://gpu:11434"})
        assert result == {"ok": False, "error": "apply_failed",
                          "message": "Nova could not apply the AI settings."}
        assert runtime.runtime_config == before
    assert ai_fakes.tests == [] and ai_fakes.saves == []
    assert ai_fakes.invalidations == [] and ai_fakes.reloads == []


async def test_apply_uses_live_runtime_via_a_snapshot_then_updates_runtime(
    hass, hass_ws_client, ai_fakes, gate,
):
    entry = await _setup(hass)
    live = entry.runtime_data.runtime_config
    # Only the live runtime knows this endpoint; the update omits it.
    live["ollama_base_url"] = "http://live-gpu:11434"
    with _bridge(hass, entry, "drifted"):
        result = await _apply(hass, hass_ws_client, _ollama_roles("qwen"))
    assert result == {"ok": True, "message": "AI settings saved. Nova is reloading."}
    given, when_given = gate.seen[-1]
    assert given is not live
    assert when_given["ollama_base_url"] == "http://live-gpu:11434"
    assert ai_fakes.tests == [("ollama", "qwen", "http://live-gpu:11434")]
    saved = ai_fakes.saves[-1]
    assert saved["llm_base_url"] == "" and saved["self_hosted_endpoints_migrated"] is True
    for key, value in saved.items():
        assert live[key] == value                        # live runtime updated
    assert hass.data[DOMAIN][entry.entry_id]["runtime_config"] is live
    assert ai_fakes.invalidations == [None]
    assert ai_fakes.reloads == [entry.entry_id]


@pytest.mark.parametrize("failure", ["validation", "connection", "persist"])
async def test_failed_apply_leaves_runtime_alone(
    hass, hass_ws_client, ai_fakes, failure,
):
    entry = await _setup(hass)
    live = entry.runtime_data.runtime_config
    before = dict(live)
    updates = {**_ollama_roles(), "ollama_base_url": "http://gpu:11434"}
    if failure == "validation":
        updates["ollama_base_url"] = ""
        expected_error = "invalid_configuration"
    elif failure == "connection":
        ai_fakes.test_result = "cannot_connect"
        expected_error = "connection_test_failed"
    else:
        ai_fakes.save_result = False
        expected_error = "persist_failed"
    result = await _apply(hass, hass_ws_client, updates)
    assert result["ok"] is False and result["error"] == expected_error
    assert live == before
    assert ai_fakes.invalidations == [] and ai_fakes.reloads == []
    if failure != "persist":
        assert ai_fakes.saves == []


# ── nova/list_models and nova/get_credential_status ─────────────────────────

async def test_list_models_uses_live_runtime_endpoint(
    hass, hass_ws_client, executor_fakes,
):
    entry = await _setup(hass)
    entry.runtime_data.runtime_config["ollama_base_url"] = "http://live-gpu:11434"
    with _bridge(hass, entry, "drifted"):
        resp = await _ws(hass, hass_ws_client,
                         {"type": "nova/list_models", "provider": "ollama"})
    assert resp["success"], resp
    assert resp["result"] == {"provider": "ollama", "models": ["m1"],
                              "model_details": {}, "cached": False, "truncated": False}
    assert executor_fakes["models"][-1]["ollama_base_url"] == "http://live-gpu:11434"


async def test_list_models_without_runtime_returns_the_safe_error(
    hass, hass_ws_client, executor_fakes,
):
    entry = await _setup(hass)
    with _runtime_missing(entry):
        resp = await _ws(hass, hass_ws_client,
                         {"type": "nova/list_models", "provider": "ollama"})
    assert resp["success"], resp
    assert resp["result"] == {"provider": "ollama", "models": [],
                              "error": "model_discovery_unavailable"}
    assert executor_fakes["models"] == []


async def test_credential_status_uses_live_runtime_endpoints(hass, hass_ws_client):
    entry = await _setup(hass)
    rc = entry.runtime_data.runtime_config
    rc["self_hosted_endpoints_migrated"] = True
    rc["ollama_base_url"] = "http://live-gpu:11434"
    # The drifted bridge has a custom endpoint; the runtime does not.
    with _bridge(hass, entry, "drifted"):
        resp = await _ws(hass, hass_ws_client, {"type": "nova/get_credential_status"})
    assert resp["success"], resp
    result = resp["result"]
    assert set(result) == {"status", "available"}
    assert set(result["available"]) == {"groq", "openai", "anthropic", "gemini",
                                        "custom", "ollama"}
    assert result["available"]["ollama"] is True
    assert result["available"]["custom"] is False
    assert all(type(v) is bool for v in result["status"].values())


async def test_credential_status_without_runtime_returns_empty(hass, hass_ws_client):
    entry = await _setup(hass)
    with _runtime_missing(entry):
        resp = await _ws(hass, hass_ws_client, {"type": "nova/get_credential_status"})
    assert resp["success"], resp
    assert resp["result"] == {"status": {}, "available": {}}
