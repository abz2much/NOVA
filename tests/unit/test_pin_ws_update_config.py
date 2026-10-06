"""Pin what nova/update_config does today (8.7.22, tests only).

nova/update_config is the panel's one generic config write. It checks the key
against PANEL_WRITABLE_KEYS, refuses credential keys, runs
safety_config.valid_panel_value, then writes runtime_config and config.json
and applies a few keys live. The decorators are stubbed to pass through, so
the handler body runs with a recording connection, a fake entry and a fake
config store. Tests named test_current_behaviour_* pin behaviour that looks
wrong; each says why. Nothing here changes production code.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


class _Conn:
    """A websocket connection that records what the handler sends back."""

    def __init__(self):
        self.user = types.SimpleNamespace(id="admin-1", name="Abi")
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message=""):
        self.errors.append((msg_id, code, message))


def _stub_ws_api(monkeypatch):
    api = types.ModuleType("homeassistant.components.websocket_api")
    api.websocket_command = lambda schema: (lambda fn: fn)
    api.async_response = lambda fn: fn
    api.require_admin = lambda fn: fn
    api.ActiveConnection = _Conn
    api.async_register_command = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", api)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "websocket_api", api,
                        raising=False)


@pytest.fixture
def ws(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    sys.modules.pop("jc.websocket", None)
    mod = load("websocket")
    yield mod
    sys.modules.pop("jc.websocket", None)


@pytest.fixture
def store(load, monkeypatch):
    """config.json, as a dict. `store.ok` is what nova_config.set returns."""
    nc = load("nova_config")
    data: dict = {}
    state = types.SimpleNamespace(data=data, ok=True, calls=[])

    def _set(key, value):
        state.calls.append((key, value))
        data[key] = value
        return state.ok
    monkeypatch.setattr(nc, "set", _set)
    monkeypatch.setattr(nc, "get", lambda k, d=None: data.get(k, d))
    return state


@pytest.fixture
def observer(load, monkeypatch):
    """The observer module with start/stop/is_running recorded."""
    ob = load("observer")
    st = types.SimpleNamespace(running=False, starts=[], stops=0, refreshed=[],
                               start_works=True, stop_works=True)

    async def start(hass, config, entry=None):
        st.starts.append(config)
        if st.start_works:
            st.running = True

    async def stop():
        st.stops += 1
        if st.stop_works:
            st.running = False

    async def refresh(hass, updates=None):
        st.refreshed.append(updates)
    monkeypatch.setattr(ob, "start", start)
    monkeypatch.setattr(ob, "stop", stop)
    monkeypatch.setattr(ob, "is_running", lambda: st.running)
    monkeypatch.setattr(ob, "refresh_tier_providers", refresh)
    return st


@pytest.fixture
def applied(load, monkeypatch):
    """Keys cognitive_core.apply_runtime_config was asked to apply live."""
    cc = load("cognitive_core")
    seen = []

    async def apply_runtime_config(key, value):
        seen.append((key, value))
    monkeypatch.setattr(cc, "apply_runtime_config", apply_runtime_config)
    return seen


@pytest.fixture
def audit(load, monkeypatch):
    """Every Action Audit Log write. The panel config write makes none today."""
    al = load("action_log")
    rows = []
    for name in ("start", "start_many", "set_approval", "set_execution"):
        monkeypatch.setattr(al, name, lambda *a, _n=name, **k: rows.append((_n, a, k)))
    return rows


def _entry(load, runtime_config=None, with_runtime=True):
    rt_mod = load("runtime")
    runtime = None
    if with_runtime:
        runtime = rt_mod.NovaRuntime(
            client=None, llm_provider_name="groq", sentinel=None,
            reminder_watcher=None, scheduler=None, resources=None,
            automation_contexts=None,
            runtime_config=dict(runtime_config or {}))
    return types.SimpleNamespace(entry_id="e1", options={}, data={},
                                 runtime_data=runtime, state=None)


def _hass(entry):
    hass = FakeHass()
    hass.config_entries = types.SimpleNamespace(
        async_entries=lambda domain: [entry] if entry is not None else [])
    return hass


async def _update(ws, hass, key, value, msg_id=1):
    conn = _Conn()
    await ws.ws_update_config(hass, conn, {"id": msg_id, "key": key, "value": value})
    return conn


# ── What is refused before anything is written ─────────────────────────────

@pytest.mark.parametrize("key", ["api_key", "gemini_api_key", "anthropic_api_key"])
async def test_a_credential_key_is_refused_and_nothing_is_written(ws, load, store, key):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), key, "sk-secret")
    assert conn.results == []
    (_, code, message), = conn.errors
    assert code == "invalid_key"
    assert "nova/set_credential" in message and "sk-secret" not in message
    assert store.calls == [] and entry.runtime_data.runtime_config == {}


async def test_every_credential_key_is_refused_even_if_added_to_the_allowlist(
        ws, load, store, monkeypatch):
    # The credential check runs before the allowlist, so an edit that puts
    # a credential in PANEL_WRITABLE_KEYS still cannot write it.
    creds = load("ha_secrets").CREDENTIAL_KEYS
    monkeypatch.setattr(ws, "PANEL_WRITABLE_KEYS", set(ws.PANEL_WRITABLE_KEYS) | set(creds))
    entry = _entry(load)
    for key in creds:
        conn = await _update(ws, _hass(entry), key, "x")
        assert conn.errors[0][1] == "invalid_key"
    assert store.calls == []


async def test_a_key_outside_the_allowlist_is_refused_by_name(ws, load, store):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "camera_overrides", "{}")
    assert conn.errors == [(1, "invalid_key", "Key 'camera_overrides' is not writable from the panel")]
    assert store.calls == [] and entry.runtime_data.runtime_config == {}


def test_no_credential_key_is_in_the_allowlist(ws, load):
    assert not set(load("ha_secrets").CREDENTIAL_KEYS) & ws.PANEL_WRITABLE_KEYS


@pytest.mark.parametrize("key", ["lockdown_auto_on_arm", "intrusion_requires_confinement",
                                 "face_stand_down", "scene_memory_enabled",
                                 "camera_event_learning", "host_health_enabled"])
@pytest.mark.parametrize("value", ["true", 1, "yes", None])
async def test_a_safety_opt_in_needs_a_real_boolean(ws, load, store, applied, key, value):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), key, value)
    assert conn.errors == [(1, "invalid_value", f"Key '{key}' requires a boolean value")]
    assert store.calls == [] and applied == []
    assert entry.runtime_data.runtime_config == {}


async def test_an_unsupported_output_language_gets_its_own_message(ws, load, store):
    conn = await _update(ws, _hass(_entry(load)), "output_language", "klingon")
    assert conn.errors == [(1, "invalid_value",
                            "Key 'output_language' must be 'auto' or a supported language code")]
    assert store.calls == []


@pytest.mark.parametrize("key,value", [
    ("scene_memory_retention_days", 0),
    ("scene_memory_retention_days", 91),
    ("camera_awareness_min_observations", 2),
    ("camera_event_confidence_floor", 101.0),
    ("camera_event_dedup_window", -1),
    ("host_health_persistence_minutes", 1),
    ("host_health_cooldown_minutes", 721),
    ("notify_services", '["light.kitchen"]'),
    ("notify_services", "notify.phone"),
    ("host_health_mappings", '{"cpu": "light.kitchen"}'),
    ("host_health_thresholds", '{"not_a_metric": 50}'),
])
async def test_bounded_and_json_values_out_of_range_are_refused(ws, load, store, key, value):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), key, value)
    assert conn.errors and conn.errors[0][1] == "invalid_value"
    assert store.calls == [] and entry.runtime_data.runtime_config == {}


async def test_current_behaviour_a_bad_number_is_told_it_needs_a_boolean(ws, load, store):
    # Looks wrong: every refusal except output_language says "requires a
    # boolean value", so a retention of 500 days is refused with a message
    # that sends the user looking for a toggle.
    conn = await _update(ws, _hass(_entry(load)), "scene_memory_retention_days", 500)
    assert conn.errors == [(1, "invalid_value",
                            "Key 'scene_memory_retention_days' requires a boolean value")]


async def test_no_entry_is_refused_after_validation(ws, store):
    conn = await _update(ws, _hass(None), "announcements_enabled", True)
    assert conn.errors == [(1, "no_entry", "No Nova config entry found")]
    assert store.calls == []


async def test_an_entry_with_no_runtime_fails_before_any_write(ws, load, store, observer):
    entry = _entry(load, with_runtime=False)
    conn = await _update(ws, _hass(entry), "observer_enabled", True)
    (_, code, message), = conn.errors
    assert code == "update_failed"
    # safe_errors: the type only, never the raw text (it names the entry id).
    assert message == "NovaRuntimeUnavailable (details are in the Home Assistant log)"
    assert store.calls == [] and observer.starts == []


async def test_a_runtime_without_a_dict_config_is_no_data(ws, load, store):
    entry = _entry(load)
    entry.runtime_data.runtime_config = None
    conn = await _update(ws, _hass(entry), "announcements_enabled", True)
    assert conn.errors == [(1, "no_data", "Nova runtime data not found")]
    assert store.calls == []


# ── A good write ────────────────────────────────────────────────────────────

async def test_a_good_write_lands_in_runtime_config_and_config_json(ws, load, store):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "announcements_enabled", False)
    assert conn.errors == []
    assert conn.results == [(1, {"key": "announcements_enabled", "value": False,
                                 "persisted": True})]
    assert entry.runtime_data.runtime_config == {"announcements_enabled": False}
    assert store.data == {"announcements_enabled": False}


async def test_a_failed_save_is_reported_and_the_session_value_stays(ws, load, store):
    store.ok = False
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "banter_level", 2)
    assert conn.results == [(1, {"key": "banter_level", "value": 2, "persisted": False})]
    assert entry.runtime_data.runtime_config["banter_level"] == 2


async def test_a_save_that_raises_is_reported_as_not_persisted(ws, load, monkeypatch):
    nc = load("nova_config")

    def boom(k, v):
        raise OSError("/config/nova/config.json: disk full")
    monkeypatch.setattr(nc, "set", boom)
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "banter_level", 0)
    assert conn.results == [(1, {"key": "banter_level", "value": 0, "persisted": False})]


@pytest.mark.parametrize("key", ["lockdown_auto_on_arm", "intrusion_requires_confinement",
                                 "face_stand_down"])
async def test_the_safety_opt_ins_are_applied_live(ws, load, store, applied, key):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), key, True)
    assert conn.results[0][1]["value"] is True
    assert applied == [(key, True)]


async def test_an_ordinary_key_is_not_applied_to_the_core(ws, load, store, applied):
    await _update(ws, _hass(_entry(load)), "banter_level", 1)
    assert applied == []


async def test_turning_cognition_off_marks_the_gap(ws, load, store, monkeypatch):
    cog = load("cognition")
    marks = []
    monkeypatch.setattr(cog, "mark_unobserved", lambda: marks.append(1))
    await _update(ws, _hass(_entry(load)), "cognition_enabled", False)
    await _update(ws, _hass(_entry(load)), "cognition_enabled", True)
    assert marks == [1]


@pytest.mark.parametrize("key,dropped", [
    ("llm_base_url", ["custom", "ollama"]),
    ("ollama_base_url", ["ollama"]),
    ("custom_base_url", ["custom"]),
])
async def test_an_endpoint_change_drops_the_cached_model_lists(ws, load, store, monkeypatch,
                                                                key, dropped):
    seen = []
    monkeypatch.setattr(ws, "invalidate_model_cache", lambda p=None: seen.append(p))
    await _update(ws, _hass(_entry(load)), key, "http://192.168.1.5:11434")
    assert seen == dropped


async def test_a_tier_model_change_refreshes_a_running_observer(ws, load, store, observer):
    observer.running = True
    await _update(ws, _hass(_entry(load)), "classifier_model", "llama-3.1-8b")
    assert observer.refreshed == [{"classifier_model": "llama-3.1-8b"}]
    observer.running = False
    await _update(ws, _hass(_entry(load)), "reasoning_model", "x")
    assert len(observer.refreshed) == 1


async def test_sleep_override_also_schedules_its_expiry(ws, load, store, monkeypatch):
    sd = load("sleep_detection")
    calls = []
    monkeypatch.setattr(sd, "set_override", lambda v, q: calls.append((v, q)))
    monkeypatch.setattr(ws, "sleep_detection", sd)
    store.data["observer_quiet_end"] = "06:30"
    conn = await _update(ws, _hass(_entry(load)), "sleep_override", "asleep")
    assert calls == [("asleep", "06:30")]
    assert conn.results[0][1]["persisted"] is True


async def test_current_behaviour_an_invalid_sleep_override_is_stored_and_reported_ok(
        ws, load, store, monkeypatch):
    # Looks wrong: sleep_override has no validator, so "sleepy" is written to
    # runtime_config and config.json. set_override then refuses it (a
    # ValueError, logged and swallowed) and the panel is told it worked.
    sd = load("sleep_detection")
    monkeypatch.setattr(ws, "sleep_detection", sd)
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "sleep_override", "sleepy")
    assert conn.errors == []
    assert conn.results == [(1, {"key": "sleep_override", "value": "sleepy", "persisted": True})]
    assert store.data["sleep_override"] == "sleepy"
    assert entry.runtime_data.runtime_config["sleep_override"] == "sleepy"


# ── observer_enabled is transactional ──────────────────────────────────────

async def test_enabling_the_observer_starts_it_then_records_it(ws, load, store, observer):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "observer_enabled", True)
    assert conn.results[0][1] == {"key": "observer_enabled", "value": True, "persisted": True}
    assert len(observer.starts) == 1 and observer.starts[0]["observer_enabled"] is True
    assert entry.runtime_data.observer_running is True
    assert store.data == {"observer_enabled": True}


async def test_an_observer_that_will_not_start_changes_nothing(ws, load, store, observer):
    observer.start_works = False
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "observer_enabled", True)
    assert conn.errors == [(1, "update_failed", "RuntimeError (details are in the Home Assistant log)")]
    assert entry.runtime_data.observer_running is False
    assert entry.runtime_data.runtime_config == {} and store.calls == []


async def test_a_running_observer_is_never_restarted(ws, load, store, observer):
    observer.running = True
    await _update(ws, _hass(_entry(load)), "observer_enabled", True)
    assert observer.starts == []


async def test_an_observer_that_will_not_stop_changes_nothing(ws, load, store, observer):
    observer.running, observer.stop_works = True, False
    entry = _entry(load, runtime_config={"observer_enabled": True})
    entry.runtime_data.observer_running = True
    conn = await _update(ws, _hass(entry), "observer_enabled", False)
    assert conn.errors[0][1] == "update_failed"
    assert entry.runtime_data.runtime_config == {"observer_enabled": True}
    assert entry.runtime_data.observer_running is True and store.calls == []


async def test_disabling_a_stopped_observer_does_not_stop_it_again(ws, load, store, observer):
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "observer_enabled", False)
    assert observer.stops == 0 and conn.results[0][1]["value"] is False


async def test_current_behaviour_the_string_false_starts_the_observer(ws, load, store, observer):
    # Looks wrong: observer_enabled has no validator and the handler tests
    # `if value:`, so the string "false" starts the observer and is stored.
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "observer_enabled", "false")
    assert conn.errors == []
    assert len(observer.starts) == 1 and observer.running is True
    assert store.data == {"observer_enabled": "false"}


# ── Writes that skip a check (found, not fixed) ────────────────────────────

@pytest.mark.parametrize("value", ["false", "off", 0.0001, "no"])
async def test_current_behaviour_voice_confirm_enabled_takes_any_value(ws, load, store, value):
    # Looks wrong: voice_confirm_enabled guards locks, garage doors and the
    # alarm, yet it has no validator. voice_confirm.is_enabled() reads it as
    # bool(value), so these all store a value the panel meant as "off" but
    # that reads as on (or the reverse for 0 and "").
    entry = _entry(load)
    conn = await _update(ws, _hass(entry), "voice_confirm_enabled", value)
    assert conn.errors == []
    assert store.data["voice_confirm_enabled"] == value


async def test_current_behaviour_turning_voice_confirm_off_writes_no_audit_row(
        ws, load, store, audit):
    # Looks wrong: switching off voice confirmation for locks and the alarm is
    # a safety change, but it leaves only an INFO log line. No Action Audit
    # Log row, no nova_log entry.
    conn = await _update(ws, _hass(_entry(load)), "voice_confirm_enabled", False)
    assert conn.results[0][1]["value"] is False
    assert audit == []


@pytest.mark.parametrize("key", ["lockdown_auto_on_arm", "face_stand_down",
                                 "intrusion_requires_confinement"])
async def test_current_behaviour_a_safety_opt_in_change_writes_no_audit_row(
        ws, load, store, applied, audit, key):
    # Same gap for the intrusion and lockdown opt-ins.
    await _update(ws, _hass(_entry(load)), key, True)
    assert applied == [(key, True)] and audit == []


@pytest.mark.parametrize("value", [-1, 0, 999999, "soon"])
async def test_current_behaviour_intrusion_response_timeout_is_not_bounded(ws, load, store, value):
    # Looks wrong: this is how long an unanswered intrusion waits before the
    # "couldn't reach you" notice. Any value is stored; 999999 means the
    # timed-out notice never comes, and "soon" falls back to the default.
    conn = await _update(ws, _hass(_entry(load)), "intrusion_response_timeout", value)
    assert conn.errors == [] and store.data["intrusion_response_timeout"] == value


async def test_current_behaviour_security_alarm_entity_takes_any_entity(ws, load, store, applied):
    # Looks wrong: the alarm panel automatic lockdown follows is not checked
    # to be an alarm_control_panel (or to exist), so a typo means lockdown
    # never follows the alarm, with no error.
    conn = await _update(ws, _hass(_entry(load)), "security_alarm_entity", "light.kitchen")
    assert conn.errors == []
    assert applied == [("security_alarm_entity", "light.kitchen")]


@pytest.mark.parametrize("key,value", [
    ("llm_provider", "not_a_provider"),
    ("model", ""),
    ("ollama_base_url", "http://169.254.169.254/latest"),
    ("custom_base_url", "ftp://example"),
    ("llm_base_url", "http://user:secret@10.0.0.2:11434"),
])
async def test_current_behaviour_ai_settings_bypass_the_apply_checks(
        ws, load, store, monkeypatch, key, value):
    # Looks wrong: nova/apply_ai_config validates providers and models,
    # normalises endpoints, and treats llm_base_url as server owned. The same
    # keys are in PANEL_WRITABLE_KEYS, so nova/update_config writes them with
    # none of those checks (a metadata address, a bad scheme, credentials in
    # a URL, an unknown provider, an empty model).
    monkeypatch.setattr(ws, "invalidate_model_cache", lambda p=None: None)
    conn = await _update(ws, _hass(_entry(load)), key, value)
    assert conn.errors == [] and store.data[key] == value


async def test_current_behaviour_a_value_has_no_size_limit(ws, load, store):
    # Looks wrong: free text values are not bounded. A 2 MB floor plan
    # background is written to runtime_config and config.json as is, and
    # rewritten on every save.
    big = "x" * (2 * 1024 * 1024)
    conn = await _update(ws, _hass(_entry(load)), "floor_plan_bg", big)
    assert conn.errors == [] and len(store.data["floor_plan_bg"]) == len(big)


async def test_a_failure_after_validation_returns_no_raw_exception_text(ws, load, store, monkeypatch):
    cc = load("cognitive_core")

    async def boom(key, value):
        raise RuntimeError("token=abc123 at /config/nova/secret")
    monkeypatch.setattr(cc, "apply_runtime_config", boom)
    conn = await _update(ws, _hass(_entry(load)), "face_stand_down", True)
    (_, code, message), = conn.errors
    assert code == "update_failed"
    assert "abc123" not in message and message.startswith("RuntimeError")
