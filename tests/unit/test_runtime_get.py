"""nova_config.runtime_get — the canonical per-key config resolver.

Precedence must match effective_config: runtime_config (panel-live) → config.json
→ entry.options → entry.data → default. The bug this fixes: on a panel-configured
install entry.options/data are empty, so a direct read silently returns the
default for anything set via the panel/config.json — runtime_get consults
config.json so those values are honoured.

Phase 3B: runtime_config is the entry's NovaRuntime.runtime_config, never the
hass.data bridge. A bridge that is missing, damaged or out of step has no
effect; an entry that is not loaded reads the lower layers; a loaded entry
without its runtime raises instead of answering from defaults.
"""
import types

import pytest


@pytest.fixture
def jc(load):
    m = load("nova_config")
    m._loaded = True
    m._cache = {}
    return m


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


def test_config_json_wins_over_entry(jc, const):
    # config.json overrides the entry (matches effective_config precedence)
    jc._cache = {"honorific": "monsieur"}
    entry = _Entry(options={"honorific": "sir"}, data={})
    assert jc.runtime_get(_hass(const), entry, "honorific", "d") == "monsieur"


def test_entry_used_on_yaml_install(jc, const):
    # nothing in runtime_config or config.json → options, then data
    jc._cache = {}
    entry = _Entry(options={"notify_service": "notify.opts"}, data={})
    assert jc.runtime_get(_hass(const), entry, "notify_service", "d") == "notify.opts"
    entry2 = _Entry(options={}, data={"notify_service": "notify.data"})
    assert jc.runtime_get(_hass(const), entry2, "notify_service", "d") == "notify.data"


def test_full_precedence_order(jc, const, rt, states):
    """Each layer only answers when every higher one is silent."""
    key = "k"
    layers = {"runtime": "R", "json": "J", "options": "O", "data": "D"}

    def read(active):
        jc._cache = {key: layers["json"]} if "json" in active else {}
        entry = _loaded(
            rt, states, {key: layers["runtime"]} if "runtime" in active else {},
            options={key: layers["options"]} if "options" in active else {},
            data={key: layers["data"]} if "data" in active else {})
        return jc.runtime_get(_hass(const), entry, key, "default")

    assert read({"runtime", "json", "options", "data"}) == "R"
    assert read({"json", "options", "data"}) == "J"
    assert read({"options", "data"}) == "O"
    assert read({"data"}) == "D"
    assert read(set()) == "default"


def test_default_when_nowhere(jc, const):
    jc._cache = {}
    assert jc.runtime_get(_hass(const), _Entry(), "missing", "fallback") == "fallback"


def test_empty_values_do_not_win(jc, const):
    # a blank config.json value must not clobber a real entry value
    jc._cache = {"broadcast_group": ""}
    entry = _Entry(options={"broadcast_group": "media_player.home"}, data={})
    assert jc.runtime_get(_hass(const), entry, "broadcast_group", "d") == "media_player.home"


def test_false_is_preserved(jc, const):
    # boolean False from config.json must be returned, not treated as "unset"
    jc._cache = {"sentinel_enabled": False}
    assert jc.runtime_get(_hass(const), _Entry(), "sentinel_enabled", True) is False


def test_tolerates_missing_hass_and_entry(jc, const):
    jc._cache = {"honorific": "madam"}
    assert jc.runtime_get(None, None, "honorific", "d") == "madam"   # config.json still consulted
    assert jc.runtime_get(None, None, "missing", "d") == "d"


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
