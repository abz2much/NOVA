"""nova_config.runtime_get: one setting, in effective_config order.

The order is the panel's live value, config.json, entry.options, entry.data,
then the default. A panel install has empty options and data, so reading
the entry directly gave the default for every panel setting; runtime_get
reads config.json too.

The live values come from the entry's own NovaRuntime and never from the
old hass.data bridge, whatever state that bridge is in. An entry that is
not loaded answers from the lower layers; a loaded entry with no runtime
raises rather than answer from defaults.
"""
import types

import pytest


@pytest.fixture
def jc(load, monkeypatch):
    mod = load("nova_config")
    monkeypatch.setattr(mod, "_loaded", True)
    monkeypatch.setattr(mod, "_cache", {})
    return mod


@pytest.fixture
def const(load):
    return load("const")


@pytest.fixture
def rt(load):
    return load("runtime")


@pytest.fixture
def states():
    import sys
    return sys.modules["homeassistant.config_entries"].ConfigEntryState


class _Entry:
    def __init__(self, options=None, data=None, entry_id="e1", runtime=None,
                 state=None):
        self.options = options or {}
        self.data = data or {}
        self.entry_id = entry_id
        self.state = state
        if runtime is not None:
            self.runtime_data = runtime


def _runtime(rt, runtime_config):
    return rt.NovaRuntime(
        client=object(), llm_provider_name="groq", sentinel=object(),
        reminder_watcher=object(), scheduler=object(), resources=object(),
        automation_contexts=object(), runtime_config=runtime_config)


def _loaded(rt, states, runtime_config=None, **kw):
    """A LOADED entry that owns a NovaRuntime with this runtime_config."""
    return _Entry(runtime=_runtime(rt, {} if runtime_config is None else runtime_config),
                  state=states.LOADED, **kw)


def _hass(const, bridge_rc=None, entry_id="e1"):
    """A hass whose bridge (if any) holds `bridge_rc`: production never reads it."""
    h = types.SimpleNamespace(data={})
    if bridge_rc is not None:
        h.data = {const.DOMAIN: {entry_id: {"runtime_config": bridge_rc}}}
    return h


# ── Precedence (unchanged) ───────────────────────────────────────────────────

def test_config_json_consulted_on_panel_install(jc, const):
    # the bug: empty options/data, value only in config.json → must NOT default
    jc._cache = {"vision_model": "qwen/qwen3.6-27b"}
    entry = _Entry(options={}, data={})
    assert jc.runtime_get(_hass(const), entry, "vision_model", "DEFAULT") == "qwen/qwen3.6-27b"


def test_runtime_config_wins_over_config_json(jc, const, rt, states):
    jc._cache = {"vision_model": "from_json"}
    entry = _loaded(rt, states, {"vision_model": "from_panel_live"})
    assert jc.runtime_get(_hass(const), entry, "vision_model", "d") == "from_panel_live"


def test_config_json_beats_the_entry(jc, const):
    jc._cache = {"honorific": "madam"}
    entry = _Entry(options={"honorific": "sir"}, data={"honorific": "sir"})
    assert jc.runtime_get(_hass(const), entry, "honorific", "d") == "madam"


@pytest.mark.parametrize("layer", ["options", "data"])
def test_an_install_without_the_panel_reads_the_entry(jc, const, layer):
    entry = _Entry(**{layer: {"notify_service": f"notify.{layer}"}})
    assert jc.runtime_get(_hass(const), entry, "notify_service", "d") == f"notify.{layer}"


@pytest.mark.parametrize("present, expected", [
    (("live", "file", "options", "data"), "L"),
    (("file", "options", "data"), "F"),
    (("options", "data"), "O"),
    (("data",), "D"),
    ((), "fallback"),
])
def test_each_layer_answers_only_when_those_above_are_silent(
        jc, const, rt, states, present, expected):
    def maybe(name, value):
        return {"k": value} if name in present else {}
    jc._cache = maybe("file", "F")
    entry = _loaded(rt, states, maybe("live", "L"),
                    options=maybe("options", "O"), data=maybe("data", "D"))
    assert jc.runtime_get(_hass(const), entry, "k", "fallback") == expected


def test_nothing_anywhere_gives_the_default(jc, const):
    assert jc.runtime_get(_hass(const), _Entry(), "absent", "fallback") == "fallback"


def test_a_blank_config_json_value_does_not_hide_the_entry(jc, const):
    jc._cache = {"broadcast_group": ""}
    entry = _Entry(options={"broadcast_group": "media_player.everywhere"})
    assert jc.runtime_get(_hass(const), entry, "broadcast_group", "d") \
        == "media_player.everywhere"


def test_false_in_config_json_is_an_answer(jc, const):
    jc._cache = {"sentinel_enabled": False}
    assert jc.runtime_get(_hass(const), _Entry(), "sentinel_enabled", True) is False


def test_no_hass_and_no_entry_still_reads_config_json(jc):
    jc._cache = {"honorific": "madam"}
    assert jc.runtime_get(None, None, "honorific", "d") == "madam"
    assert jc.runtime_get(None, None, "absent", "d") == "d"


# ── runtime_config comes from NovaRuntime ────────────────────────────────────

@pytest.mark.parametrize("value", [False, 0, [], {}])
def test_falsy_runtime_values_are_valid(jc, const, rt, states, value):
    jc._cache = {"k": "from_json"}
    entry = _loaded(rt, states, {"k": value})
    got = jc.runtime_get(_hass(const), entry, "k", "d")
    assert got == value and type(got) is type(value)


@pytest.mark.parametrize("value", [None, ""])
def test_none_and_blank_runtime_values_fall_through(jc, const, rt, states, value):
    jc._cache = {"k": "from_json"}
    entry = _loaded(rt, states, {"k": value})
    assert jc.runtime_get(_hass(const), entry, "k", "d") == "from_json"


def test_live_runtime_change_is_seen_on_the_next_read(jc, const, rt, states):
    live = {"vision_model": "first"}
    entry = _loaded(rt, states, live)
    assert jc.runtime_get(_hass(const), entry, "vision_model", "d") == "first"
    live["vision_model"] = "second"          # a panel write, in place
    assert jc.runtime_get(_hass(const), entry, "vision_model", "d") == "second"


def test_bridge_value_is_ignored(jc, const, rt, states):
    # Only the bridge has the key: the runtime is authoritative, so fall through.
    jc._cache = {"vision_model": "from_json"}
    entry = _loaded(rt, states, {})
    h = _hass(const, bridge_rc={"vision_model": "from_bridge"})
    assert jc.runtime_get(h, entry, "vision_model", "d") == "from_json"


def test_drifted_bridge_is_ignored(jc, const, rt, states):
    entry = _loaded(rt, states, {"vision_model": "from_runtime"})
    h = _hass(const, bridge_rc={"vision_model": "stale_bridge"})
    assert jc.runtime_get(h, entry, "vision_model", "d") == "from_runtime"


@pytest.mark.parametrize("store", [None, "junk", {"e1": "junk"}, {"e1": {"runtime_config": "junk"}}])
def test_damaged_bridge_has_no_effect(jc, const, rt, states, store):
    entry = _loaded(rt, states, {"vision_model": "from_runtime"})
    h = types.SimpleNamespace(data={} if store is None else {const.DOMAIN: store})
    assert jc.runtime_get(h, entry, "vision_model", "d") == "from_runtime"


def test_hass_none_skips_runtime(jc, rt, states):
    jc._cache = {"vision_model": "from_json"}
    entry = _loaded(rt, states, {"vision_model": "from_runtime"})
    assert jc.runtime_get(None, entry, "vision_model", "d") == "from_json"


def test_entry_none_is_safe(jc, const):
    jc._cache = {"vision_model": "from_json"}
    assert jc.runtime_get(_hass(const), None, "vision_model", "d") == "from_json"


def test_loaded_entry_without_runtime_raises(jc, const, rt, states):
    jc._cache = {"vision_model": "from_json"}
    entry = _Entry(options={"vision_model": "from_options"}, state=states.LOADED)
    # Even a healthy-looking bridge must not stand in for the lost runtime.
    h = _hass(const, bridge_rc={"vision_model": "from_bridge"})
    with pytest.raises(rt.NovaRuntimeUnavailable):
        jc.runtime_get(h, entry, "vision_model", "d")


@pytest.mark.parametrize("state_name", ["SETUP_IN_PROGRESS", "NOT_LOADED", "SETUP_ERROR",
                                        "SETUP_RETRY", "UNLOAD_IN_PROGRESS"])
def test_entry_that_is_not_loaded_reads_lower_layers(jc, const, states, state_name):
    jc._cache = {}
    entry = _Entry(options={"vision_model": "from_options"}, state=getattr(states, state_name))
    h = _hass(const, bridge_rc={"vision_model": "from_bridge"})
    assert jc.runtime_get(h, entry, "vision_model", "d") == "from_options"


# ── Self-hosted endpoint blanks (unchanged) ──────────────────────────────────

def test_blank_runtime_endpoint_falls_through_to_config_json(jc, const, rt, states):
    jc._cache = {"ollama_base_url": "http://gpu:11434"}
    entry = _loaded(rt, states, {"ollama_base_url": ""})
    assert jc.runtime_get(_hass(const), entry, "ollama_base_url", "d") == "http://gpu:11434"


def test_migrated_blank_endpoint_beats_the_entry(jc, const, rt, states):
    # After the endpoint split, a blank in config.json means "cleared".
    jc._cache = {"self_hosted_endpoints_migrated": True, "custom_base_url": ""}
    entry = _loaded(rt, states, {}, options={"custom_base_url": "http://old"})
    assert jc.runtime_get(_hass(const), entry, "custom_base_url", "d") == ""


def test_unmigrated_blank_endpoint_uses_the_entry(jc, const, rt, states):
    jc._cache = {"custom_base_url": ""}
    entry = _loaded(rt, states, {}, options={"custom_base_url": "http://old"})
    assert jc.runtime_get(_hass(const), entry, "custom_base_url", "d") == "http://old"
