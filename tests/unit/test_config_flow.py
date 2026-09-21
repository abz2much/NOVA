"""Config-flow options steps must render fields — regression guard for the
empty "Step 1 of N" dialog (schemas had been left as stubs in an earlier build).
Stubs the minimal HA config-flow/selector surface at import."""
import sys
import types

import pytest

pytest.importorskip("voluptuous")  # core HA dep; skip cleanly where absent


def _install_stubs():
    # homeassistant.core.callback
    core = sys.modules.get("homeassistant.core") or types.ModuleType("homeassistant.core")
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    sys.modules["homeassistant.core"] = core

    # homeassistant.config_entries
    ce = types.ModuleType("homeassistant.config_entries")

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
    assert len(res["data_schema"].schema) == 6   # persona, preset, directive, model, hass-api, ui-style


async def test_step_routing_renders_fields(config_flow, fake_hass):
    res = await _flow(config_flow, fake_hass).async_step_routing(None)
    assert len(res["data_schema"].schema) == 3


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

