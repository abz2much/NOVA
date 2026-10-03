"""Config-flow options steps must render fields — regression guard for the
empty "Step 1 of N" dialog (schemas had been left as stubs in an earlier build).
Stubs the minimal HA config-flow/selector surface at import."""
import sys
import types

import pytest

# Required, not optional: these tests silently skipped for months when it was
# missing. CI's unit job installs it; locally run `pip install voluptuous`.
import voluptuous  # noqa: E402,F401


def _install_stubs():
    # homeassistant.core.callback
    core = sys.modules.get("homeassistant.core") or types.ModuleType("homeassistant.core")
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    sys.modules["homeassistant.core"] = core

    # homeassistant.config_entries
    # Extend the shared stub (tests/conftest.py) rather than replace it:
    # replacing it drops ConfigEntryState, which later test files need.
    ce = sys.modules.get("homeassistant.config_entries") or types.ModuleType("homeassistant.config_entries")

    class ConfigFlow:
        def __init_subclass__(cls, **kw):
            pass

    class OptionsFlow:
        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_show_menu(self, **kw):
            return {"type": "menu", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create_entry", **kw}

        def async_abort(self, **kw):
            return {"type": "abort", **kw}

    class ConfigEntry:
        pass

    ce.ConfigFlow = ConfigFlow
    ce.OptionsFlow = OptionsFlow
    ce.ConfigEntry = ConfigEntry
    sys.modules["homeassistant.config_entries"] = ce

    # homeassistant.helpers.selector — every selector used, as no-op callables.
    sel = types.ModuleType("homeassistant.helpers.selector")
    for name in ("TextSelector", "TextSelectorConfig", "SelectSelector",
                 "SelectSelectorConfig", "BooleanSelector", "AreaSelector",
                 "AreaSelectorConfig", "EntitySelector", "EntitySelectorConfig",
                 "NumberSelector", "NumberSelectorConfig"):
        setattr(sel, name, lambda *a, **k: object())

    class _Modes:
        DROPDOWN = "dropdown"
        SLIDER = "slider"

    class _Types:
        PASSWORD = "password"

    sel.SelectSelectorMode = _Modes
    sel.NumberSelectorMode = _Modes
    sel.TextSelectorType = _Types
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is not None:
        helpers.selector = sel
    sys.modules["homeassistant.helpers.selector"] = sel


_install_stubs()


class _Entry:
    options: dict = {}
    data: dict = {}


@pytest.fixture
def config_flow(load):
    return load("config_flow")


# ── v6.45.0: legacy add-on split removed ─────────────────────────────────────

def test_find_config_reads_runtime_path_only(config_flow, tmp_path):
    """Auto-import reads <config dir>/nova/config.json (the panel's runtime
    store, for zero-touch re-installs). The legacy add-on path is gone.
    The caller resolves config dir via hass.config.path(); _find_config
    just reads whatever path it's given."""
    assert not hasattr(config_flow, "_CONFIG_PATHS")   # legacy list removed
    runtime = tmp_path / "config.json"
    runtime.write_text('{"api_key": "gsk_test", "model": "llama"}')
    cfg = config_flow._find_config(str(runtime))
    assert cfg and cfg["api_key"] == "gsk_test"


def test_find_config_requires_usable_llm(config_flow, tmp_path):
    runtime = tmp_path / "config.json"
    runtime.write_text('{"honorific": "sir"}')        # no key, no local URL
    assert config_flow._find_config(str(runtime)) is None


def test_find_config_missing_file_is_none(config_flow, tmp_path):
    assert config_flow._find_config(str(tmp_path / "nope.json")) is None


def _flow(config_flow, fake_hass):
    flow = config_flow.NovaOptionsFlow(_Entry())
    flow.hass = fake_hass
    return flow


async def test_step_init_renders_menu(config_flow, fake_hass):
    # init is now a landing menu (not a form) — jump to any section directly
    res = await _flow(config_flow, fake_hass).async_step_init(None)
    assert res["type"] == "menu" and res["step_id"] == "init"
    assert set(res["menu_options"]) == {
        "core", "routing", "observer", "credentials", "identity", "email",
    }


async def test_step_core_renders_fields(config_flow, fake_hass):
    res = await _flow(config_flow, fake_hass).async_step_core(None)
    assert res["type"] == "form" and res["step_id"] == "core"
    # The retired UI-style switch no longer belongs in the merged panel.
    assert len(res["data_schema"].schema) == 5   # persona, preset, directive, model, hass-api


async def test_step_routing_renders_fields(config_flow, fake_hass):
    res = await _flow(config_flow, fake_hass).async_step_routing(None)
    # bedroom + ground-floor areas, broadcast group, notify service
    assert len(res["data_schema"].schema) == 4


async def test_step_observer_renders_fields(config_flow, fake_hass):
    # gemini_api_key moved to the Credentials step (Phase 2, v7.107.0) — 7 -> 6
    res = await _flow(config_flow, fake_hass).async_step_observer(None)
    assert len(res["data_schema"].schema) == 6


async def test_step_identity_renders_fields(config_flow, fake_hass):
    res = await _flow(config_flow, fake_hass).async_step_identity(None)
    assert len(res["data_schema"].schema) == 5   # enabled, voice-fp, source, auto-enroll, min-confidence


async def test_no_section_step_is_an_empty_stub(config_flow, fake_hass):
    # init is a menu now; the four section steps must each render real fields
    flow = _flow(config_flow, fake_hass)
    for step in (flow.async_step_core, flow.async_step_routing,
                 flow.async_step_observer, flow.async_step_identity):
        res = await step(None)
        assert res["type"] == "form"
        assert len(res["data_schema"].schema) > 0   # never an empty form


async def test_section_saves_independently(config_flow, fake_hass):
    # submitting a section creates the entry immediately (menu flow — each
    # section saves on its own rather than chaining to the next step)
    flow = _flow(config_flow, fake_hass)
    res = await flow.async_step_core({"honorific": "boss", "model": "x"})
    assert res["type"] == "create_entry"
    # only the submitted keys are carried in _data (other sections untouched)
    assert flow._data == {"honorific": "boss", "model": "x"}


# ── Credentials step (Phase 2, v7.107.0) ──────────────────────────────────────

_PROVIDERS = ("groq", "openai", "anthropic", "gemini", "custom", "ollama")


@pytest.fixture
def ha_secrets(load):
    return load("ha_secrets")


class _EntryWithActiveProvider(_Entry):
    data = {"llm_provider": "openai"}


async def test_step_credentials_renders_all_provider_fields(config_flow, fake_hass, ha_secrets, monkeypatch):
    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)
    res = await _flow(config_flow, fake_hass).async_step_credentials(None)
    assert res["type"] == "form" and res["step_id"] == "credentials"
    # 6 providers x (credential field + clear checkbox) + 1 confirm checkbox
    assert len(res["data_schema"].schema) == 6 * 2 + 1


async def test_step_credentials_status_shown_without_values(config_flow, fake_hass, ha_secrets, monkeypatch):
    monkeypatch.setattr(
        ha_secrets, "has_provider_credential_sync",
        lambda p, path=None: p == "anthropic",
    )
    res = await _flow(config_flow, fake_hass).async_step_credentials(None)
    status_note = res["description_placeholders"]["status"]
    assert "anthropic: configured" in status_note
    assert "groq: not set" in status_note
    # has_provider_credential_sync is boolean-only by construction (Phase 2's
    # CRUD layer never returns a value) — the note built from it structurally
    # cannot contain a credential.


async def test_step_credentials_blank_submission_writes_and_deletes_nothing(
    config_flow, fake_hass, ha_secrets, monkeypatch,
):
    writes = []
    deletes = []
    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)

    async def _fake_set(hass, provider, value):
        writes.append((provider, value))
        return True

    async def _fake_delete(hass, provider):
        deletes.append(provider)
        return True

    monkeypatch.setattr(ha_secrets, "async_set_provider_credential", _fake_set)
    monkeypatch.setattr(ha_secrets, "async_delete_provider_credential", _fake_delete)

    flow = _flow(config_flow, fake_hass)
    submission = {f"{p}_credential": "" for p in _PROVIDERS}
    submission.update({f"clear_{p}": False for p in _PROVIDERS})
    res = await flow.async_step_credentials(submission)

    assert res["type"] == "create_entry"
    assert writes == []
    assert deletes == []


async def test_step_credentials_writes_only_the_submitted_provider(
    config_flow, fake_hass, ha_secrets, monkeypatch,
):
    writes = []
    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)

    async def _fake_set(hass, provider, value):
        writes.append((provider, value))
        return True

    monkeypatch.setattr(ha_secrets, "async_set_provider_credential", _fake_set)
    monkeypatch.setattr(
        ha_secrets, "async_delete_provider_credential",
        lambda hass, provider: pytest.fail("should not be called"),
    )

    flow = _flow(config_flow, fake_hass)
    submission = {f"{p}_credential": "" for p in _PROVIDERS}
    submission.update({f"clear_{p}": False for p in _PROVIDERS})
    submission["groq_credential"] = "NEW-GROQ-KEY"
    res = await flow.async_step_credentials(submission)

    assert res["type"] == "create_entry"
    assert writes == [("groq", "NEW-GROQ-KEY")]


async def test_step_credentials_set_and_clear_same_provider_is_rejected(
    config_flow, fake_hass, ha_secrets, monkeypatch,
):
    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)
    monkeypatch.setattr(
        ha_secrets, "async_set_provider_credential",
        lambda hass, provider, value: pytest.fail("should not be called"),
    )
    monkeypatch.setattr(
        ha_secrets, "async_delete_provider_credential",
        lambda hass, provider: pytest.fail("should not be called"),
    )

    flow = _flow(config_flow, fake_hass)
    submission = {f"{p}_credential": "" for p in _PROVIDERS}
    submission.update({f"clear_{p}": False for p in _PROVIDERS})
    submission["openai_credential"] = "SOMETHING"
    submission["clear_openai"] = True
    res = await flow.async_step_credentials(submission)

    assert res["type"] == "form"
    assert res["errors"]["base"] == "credential_set_and_clear"


async def test_step_credentials_clearing_active_provider_needs_confirmation(
    config_flow, fake_hass, ha_secrets, monkeypatch,
):
    deletes = []
    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)
    monkeypatch.setattr(
        ha_secrets, "async_delete_provider_credential",
        lambda hass, provider: deletes.append(provider),
    )

    flow = config_flow.NovaOptionsFlow(_EntryWithActiveProvider())
    flow.hass = fake_hass
    submission = {f"{p}_credential": "" for p in _PROVIDERS}
    submission.update({f"clear_{p}": False for p in _PROVIDERS})
    submission["clear_openai"] = True  # openai is the active provider here
    # No confirm_delete_active -> must be rejected, nothing deleted.
    res = await flow.async_step_credentials(submission)

    assert res["type"] == "form"
    assert res["errors"]["base"] == "confirm_required_for_active_provider"
    assert deletes == []


async def test_step_credentials_clearing_active_provider_with_confirmation_succeeds(
    config_flow, fake_hass, ha_secrets, monkeypatch,
):
    deletes = []

    async def _fake_delete(hass, provider):
        deletes.append(provider)
        return True

    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)
    monkeypatch.setattr(ha_secrets, "async_delete_provider_credential", _fake_delete)

    flow = config_flow.NovaOptionsFlow(_EntryWithActiveProvider())
    flow.hass = fake_hass
    submission = {f"{p}_credential": "" for p in _PROVIDERS}
    submission.update({f"clear_{p}": False for p in _PROVIDERS})
    submission["clear_openai"] = True
    submission["confirm_delete_active"] = True
    res = await flow.async_step_credentials(submission)

    assert res["type"] == "create_entry"
    assert deletes == ["openai"]


async def test_step_credentials_clearing_non_active_provider_needs_no_confirmation(
    config_flow, fake_hass, ha_secrets, monkeypatch,
):
    deletes = []

    async def _fake_delete(hass, provider):
        deletes.append(provider)
        return True

    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)
    monkeypatch.setattr(ha_secrets, "async_delete_provider_credential", _fake_delete)

    # Active provider is openai (default entry -> "groq" via _cur default),
    # but here we clear gemini, which isn't driving the Main Agent.
    flow = _flow(config_flow, fake_hass)  # default _Entry -> active provider "groq"
    submission = {f"{p}_credential": "" for p in _PROVIDERS}
    submission.update({f"clear_{p}": False for p in _PROVIDERS})
    submission["clear_gemini"] = True
    res = await flow.async_step_credentials(submission)

    assert res["type"] == "create_entry"
    assert deletes == ["gemini"]


async def test_step_credentials_values_never_land_in_nova_config(
    config_flow, fake_hass, ha_secrets, load, monkeypatch,
):
    """Credential values submitted here must go straight to ha_secrets, never
    through nova_config.set/set_many (which would plaintext them)."""
    jc = load("nova_config")
    calls = []
    monkeypatch.setattr(jc, "set", lambda k, v: calls.append(("set", k, v)))
    monkeypatch.setattr(jc, "set_many", lambda d: calls.append(("set_many", dict(d))))
    monkeypatch.setattr(ha_secrets, "has_provider_credential_sync", lambda p, path=None: False)

    async def _fake_set(hass, provider, value):
        return True

    monkeypatch.setattr(ha_secrets, "async_set_provider_credential", _fake_set)

    flow = _flow(config_flow, fake_hass)
    submission = {f"{p}_credential": "" for p in _PROVIDERS}
    submission.update({f"clear_{p}": False for p in _PROVIDERS})
    submission["anthropic_credential"] = "sk-ant-should-not-be-plaintext"
    await flow.async_step_credentials(submission)

    assert calls == []


# ── First run: two step setup (key/address, then model) ──────────────────────

def _first_run_flow(config_flow, monkeypatch, *, lists=None, conn=None, saved=None,
                    probes=None, write_ok=True, config_ok=True):
    """NovaConfigFlow with Home Assistant's flow helpers faked and every
    network call stubbed. lists: provider -> (models, details) or None for
    an unreadable list. conn: provider -> ProviderErrorKind value returned by
    screen 1's fallback test (missing means it passed). saved: provider ->
    saved key. probes: role -> screen 3 probe result. config_ok: whether the
    config.json write succeeds."""
    import importlib
    hs = importlib.import_module("jc.ha_secrets")
    sp = importlib.import_module("jc.setup_probe")
    nc = importlib.import_module("jc.nova_config")
    paths = importlib.import_module("jc.paths")
    lists, conn, saved, probes = lists or {}, conn or {}, saved or {}, probes or {}
    calls = {"conn": [], "probe": [], "written": [], "order": [],
             "config_set": {}, "config_deleted": []}

    async def fake_probe_model(hass, job, key, base):
        calls["conn"].append((job.provider, key, job.model, base))
        return conn.get(job.provider)

    async def fake_probe_all(hass, jobs, creds):
        calls["probe"].append((dict(jobs), dict(creds)))
        return {role: probes.get(role) for role in jobs}

    async def fake_set(hass, provider, value):
        calls["order"].append("write")
        calls["written"].append((provider, value))
        return write_ok

    def fake_get(key, default=None, path=None):
        calls["order"].append("read")
        for provider, value in saved.items():
            if key == hs.secret_key_for(f"{provider}_api_key"):
                return value
        return default

    real_configure = paths.configure

    def fake_configure(hass):
        calls["order"].append("paths")
        real_configure(hass)

    def fake_set_many(values):
        calls["order"].append("config")
        calls["config_set"].update(values)
        return config_ok

    monkeypatch.setattr(sp, "probe_model", fake_probe_model)
    monkeypatch.setattr(sp, "probe_all", fake_probe_all)
    monkeypatch.setattr(hs, "async_set_provider_credential", fake_set)
    monkeypatch.setattr(hs, "get_secret_sync", fake_get)
    monkeypatch.setattr(paths, "configure", fake_configure)
    monkeypatch.setattr(nc, "configure", lambda hass: None)
    monkeypatch.setattr(nc, "set_many", fake_set_many)
    monkeypatch.setattr(nc, "delete", lambda key: calls["config_deleted"].append(key))

    class _Hass:
        class config:
            @staticmethod
            def path(*a):
                return "/nonexistent/" + "/".join(a)

        async def async_add_executor_job(self, fn, *a):
            return fn(*a)

    class Flow(config_flow.NovaConfigFlow):
        hass = _Hass()
        configured = False

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create_entry", **kw}

        def async_abort(self, **kw):
            return {"type": "abort", **kw}

        async def async_set_unique_id(self, *_a, **_k):
            return None

        def _abort_if_unique_id_configured(self, *_a, **_k):
            if self.configured:
                raise _Aborted()

        async def _discover(self, provider, value):
            return lists.get(provider)

    flow = Flow()
    flow.calls = calls
    return flow


class _Aborted(Exception):
    pass


GROQ = (["openai/gpt-oss-120b", "qwen/qwen3.6-27b", "whisper-large-v3"], [])
OLLAMA = (["llama3.2", "llava", "nomic-embed-text"], [
    {"id": "llama3.2", "capabilities": ["completion", "tools"]},
    {"id": "llava", "capabilities": ["completion", "vision"]},
    {"id": "nomic-embed-text", "capabilities": ["embedding"]},
])


def _keys(**values):
    base = {"groq_api_key": "", "anthropic_api_key": "", "openai_api_key": "",
            "gemini_api_key": "", "ollama_base_url": ""}
    base.update(values)
    return base


async def test_user_step_shows_one_field_per_provider(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch)
    form = await flow.async_step_user(None)
    assert form["step_id"] == "user"
    assert set(form["data_schema"].schema) == {
        "groq_api_key", "anthropic_api_key", "openai_api_key", "gemini_api_key",
        "ollama_base_url"}
    assert form["description_placeholders"] == {"saved": "—",
                                                 "example": "http://192.168.1.50:11434"}


async def test_already_set_up_stops_on_the_first_screen(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch)
    flow.configured = True
    with pytest.raises(_Aborted):
        await flow.async_step_user(None)


async def test_nothing_entered_asks_for_one(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch)
    res = await flow.async_step_user(_keys())
    assert res["errors"] == {"base": "need_llm"}


async def test_key_is_trimmed(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ})
    res = await flow.async_step_user(_keys(groq_api_key="  gsk_abc \n"))
    assert res["step_id"] == "roles"
    assert flow._entries == {"groq": "gsk_abc"}


async def test_bare_ollama_address_is_normalised(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"ollama": OLLAMA})
    res = await flow.async_step_user(_keys(ollama_base_url="192.168.1.50"))
    assert res["step_id"] == "roles"
    assert flow._entries == {"ollama": "http://192.168.1.50:11434"}


def _suggested_key(form, field):
    for marker in form["data_schema"].schema:
        if marker == field:
            return (marker.description or {}).get("suggested_value")
    return None


async def test_failing_typed_key_blocks_keeps_the_good_one_and_never_sends_the_bad_one_back(
        config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ, "openai": None},
                           conn={"openai": "authentication_failed"})
    res = await flow.async_step_user(_keys(groq_api_key="gsk_ok", openai_api_key="gsk_wrong"))
    assert res["step_id"] == "user"
    assert res["errors"] == {"openai_api_key": "invalid_auth"}
    assert _suggested_key(res, "groq_api_key") == "gsk_ok"
    assert _suggested_key(res, "openai_api_key") == ""


async def test_failing_ollama_address_stays_for_correcting(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"ollama": None})
    res = await flow.async_step_user(_keys(ollama_base_url="http://typo:11434"))
    assert res["errors"] == {"ollama_base_url": "cannot_connect"}
    assert _suggested_key(res, "ollama_base_url") == "http://typo:11434"


async def test_key_in_the_wrong_field_is_refused_on_that_field(config_flow, monkeypatch):
    # A Groq key in the OpenAI field: OpenAI answers 401, which Nova turns
    # into authentication_failed (proven in test_setup_probe.py), which
    # screen 1 shows as invalid_auth on that field.
    flow = _first_run_flow(config_flow, monkeypatch, lists={"openai": None},
                           conn={"openai": "authentication_failed"})
    res = await flow.async_step_user(_keys(openai_api_key="gsk_groq_key"))
    assert res["errors"] == {"openai_api_key": "invalid_auth"}


async def test_unreadable_cloud_list_falls_back_to_default_model(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"anthropic": None})
    res = await flow.async_step_user(_keys(anthropic_api_key="sk-ant"))
    assert res["step_id"] == "roles"
    assert flow.calls["conn"] == [("anthropic", "sk-ant", "claude-sonnet-5", None)]
    assert flow._lists["anthropic"] == ([], [])


async def test_rate_limit_reads_as_could_not_complete(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"gemini": None},
                           conn={"gemini": "rate_limited"})
    res = await flow.async_step_user(_keys(gemini_api_key="AIza"))
    assert res["errors"] == {"gemini_api_key": "cannot_connect"}


async def test_good_key_without_the_default_model_still_passes(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"openai": None},
                           conn={"openai": "access_denied"})
    res = await flow.async_step_user(_keys(openai_api_key="sk"))
    assert res["step_id"] == "roles"
    assert flow._lists["openai"] == ([], [])


async def test_saved_key_denied_access_is_left_out_not_a_trap(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ, "openai": None},
                           conn={"openai": "access_denied"}, saved={"openai": "sk_blocked"})
    res = await flow.async_step_user(_keys(groq_api_key="g"))
    assert res["step_id"] == "roles"
    assert "openai" not in flow._lists and flow._saved_failed == ["openai"]


async def test_empty_cloud_list_still_tests_the_key(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": ([], [])},
                           conn={"groq": "authentication_failed"})
    res = await flow.async_step_user(_keys(groq_api_key="bad"))
    assert res["errors"] == {"groq_api_key": "invalid_auth"}
    assert flow.calls["conn"] == [("groq", "bad", "openai/gpt-oss-120b", None)]


async def test_paths_are_set_before_secrets_are_read(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ})
    await flow.async_step_user(None)
    order = flow.calls["order"]
    assert order[0] == "paths" and "read" in order


async def test_ollama_needs_a_readable_list(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"ollama": None})
    res = await flow.async_step_user(_keys(ollama_base_url="http://x:11434"))
    assert res["errors"] == {"ollama_base_url": "cannot_connect"}
    assert flow.calls["conn"] == []


async def test_entries_are_tested_at_the_same_time(config_flow, monkeypatch):
    import asyncio
    running = {"now": 0, "most": 0}
    flow = _first_run_flow(config_flow, monkeypatch)

    async def slow(provider, value):
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1
        return GROQ
    flow._discover = slow
    await flow.async_step_user(_keys(groq_api_key="a", openai_api_key="b", gemini_api_key="c"))
    assert running["most"] == 3


async def test_saved_key_is_found_kept_and_tested(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ},
                           saved={"groq": "gsk_saved"})
    form = await flow.async_step_user(None)
    assert form["description_placeholders"]["saved"] == "Groq"
    res = await flow.async_step_user(_keys())
    assert res["step_id"] == "roles"
    assert flow._entries == {"groq": "gsk_saved"}
    assert flow._typed == set()


async def test_typed_key_replaces_saved_key(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ},
                           saved={"groq": "gsk_old"})
    await flow.async_step_user(_keys(groq_api_key="gsk_new"))
    assert flow._entries == {"groq": "gsk_new"}
    assert flow._typed == {"groq"}


async def test_stale_saved_key_is_left_out_without_blocking(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ, "openai": None},
                           conn={"openai": "authentication_failed"}, saved={"openai": "sk_stale"})
    res = await flow.async_step_user(_keys(groq_api_key="gsk"))
    assert res["step_id"] == "roles"
    assert flow._saved_failed == ["openai"]
    assert "openai" not in flow._lists


async def test_nothing_passing_stays_on_screen_one(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"openai": None},
                           conn={"openai": "authentication_failed"}, saved={"openai": "sk_stale"})
    res = await flow.async_step_user(_keys())
    assert res["step_id"] == "user"
    assert res["errors"] == {"base": "no_working_provider"}


async def test_resubmitting_forgets_earlier_passes(config_flow, monkeypatch):
    flow = _first_run_flow(config_flow, monkeypatch, lists={"groq": GROQ, "openai": None},
                           conn={"openai": "authentication_failed"})
    await flow.async_step_user(_keys(groq_api_key="gsk", openai_api_key="bad"))
    flow_lists = {"openai": (["gpt-5-mini"], [])}
    flow._discover = lambda p, v, _l=flow_lists: _async(_l.get(p))
    res = await flow.async_step_user(_keys(openai_api_key="good"))
    assert res["step_id"] == "roles"
    assert set(flow._lists) == {"openai"}


async def _async(value):
    return value
