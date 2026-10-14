"""The config flow and options flow, checked against their spec (7e).

These pin what the older config flow tests leave open: the exact fields,
selectors, defaults and placeholders of every screen, the order things are
read, tested and written in, the import and Supervisor discovery steps, the
options sections and the credentials section's guard rails. Every sibling
module is faked through its module attribute, and the selector helpers are
swapped for a recorder so each field's widget can be read back.

Known oddities that the rewrite keeps are pinned by tests whose names say
what happens (see the 7e bugs list)."""
from __future__ import annotations

import json
import logging
import sys
import types

import pytest
import voluptuous as vol

from fakes import FakeHass


# ── Home Assistant surface the module imports ────────────────────────────────

def _ensure_ha_flow_stubs() -> None:
    """Add the flow base classes and the selector module when no earlier test
    file did. Only missing names are added, so a stub another test file put
    in place is never replaced."""
    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    ce = sys.modules["homeassistant.config_entries"]

    class _Results:
        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_show_menu(self, **kw):
            return {"type": "menu", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create_entry", **kw}

        def async_abort(self, **kw):
            return {"type": "abort", **kw}

    class ConfigFlow(_Results):
        def __init_subclass__(cls, **kw):
            pass

    class OptionsFlow(_Results):
        pass

    for name, value in (("ConfigFlow", ConfigFlow), ("OptionsFlow", OptionsFlow),
                        ("ConfigEntry", type("ConfigEntry", (), {}))):
        if not hasattr(ce, name):
            setattr(ce, name, value)
    if "homeassistant.helpers.selector" not in sys.modules:
        sel = types.ModuleType("homeassistant.helpers.selector")
        _fill_selector(sel)
        sys.modules["homeassistant.helpers.selector"] = sel
        sys.modules["homeassistant.helpers"].selector = sel


_SELECTORS = ("TextSelector", "TextSelectorConfig", "SelectSelector", "SelectSelectorConfig",
              "BooleanSelector", "AreaSelector", "AreaSelectorConfig", "EntitySelector",
              "EntitySelectorConfig", "NumberSelector", "NumberSelectorConfig")


def _fill_selector(target) -> None:
    """Every selector and config becomes a recorder: (name, kwargs)."""
    for name in _SELECTORS:
        setattr(target, name, (lambda n: lambda *a, **k: _Made(n, a, k))(name))
    target.SelectSelectorMode = types.SimpleNamespace(DROPDOWN="dropdown")
    target.NumberSelectorMode = types.SimpleNamespace(SLIDER="slider", BOX="box")
    target.TextSelectorType = types.SimpleNamespace(PASSWORD="password")


class _Made:
    def __init__(self, name, args, kwargs):
        self.name, self.args, self.kwargs = name, args, kwargs

    def __call__(self, value):
        # voluptuous accepts any callable as a validator.
        return value

    def shape(self):
        """("Selector", {config and keyword options merged})."""
        merged = dict(self.kwargs)
        for arg in self.args:
            if isinstance(arg, _Made):
                merged.update(arg.kwargs)
        return self.name, merged


def _shape(widget):
    return widget.shape() if isinstance(widget, _Made) else widget


_ensure_ha_flow_stubs()


@pytest.fixture
def cf(load, monkeypatch):
    mod = load("config_flow")
    recorder = types.SimpleNamespace()
    _fill_selector(recorder)
    monkeypatch.setattr(mod, "selector", recorder)
    return mod


def _fields(form):
    """[(key, "Required"/"Optional", suggested value, default, widget shape)]."""
    out = []
    for marker, widget in form["data_schema"].schema.items():
        default = None if marker.default is vol.UNDEFINED else marker.default()
        suggested = (marker.description or {}).get("suggested_value")
        out.append((str(marker.schema), type(marker).__name__, suggested, default, _shape(widget)))
    return out


def _keys(form):
    return [f[0] for f in _fields(form)]


def _field(form, key):
    return next(f for f in _fields(form) if f[0] == key)


# ── a world with every sibling faked ─────────────────────────────────────────

class _Abort(Exception):
    pass


@pytest.fixture
def world(cf, load, monkeypatch, tmp_path):
    hs, sp, nc, paths = load("ha_secrets"), load("setup_probe"), load("nova_config"), load("paths")
    w = types.SimpleNamespace(order=[], saved={}, secret_reads=[], lists={}, discovered=[],
                              conn={}, conn_calls=[], probes={}, probe_calls=[],
                              written=[], write_ok=lambda p: True, deleted=[], set_many=[],
                              config_ok=True, config_dir=tmp_path)

    def get_secret(key, default=None, path=None):
        w.order.append("read")
        w.secret_reads.append(key)
        for provider, value in w.saved.items():
            if key == hs.secret_key_for(f"{provider}_api_key"):
                return value
        return default

    async def probe_model(hass, job, key, base):
        w.conn_calls.append((job.provider, job.model, job.picture, key, base))
        return w.conn.get(job.provider)

    async def probe_all(hass, jobs, creds):
        w.probe_calls.append((dict(jobs), dict(creds)))
        return {role: w.probes.get(role) for role in jobs}

    async def write(hass, provider, value):
        w.order.append("write")
        w.written.append((provider, value))
        return w.write_ok(provider)

    def set_many(values):
        w.order.append("set_many")
        w.set_many.append(dict(values))
        return w.config_ok

    monkeypatch.setattr(hs, "get_secret_sync", get_secret)
    monkeypatch.setattr(hs, "async_set_provider_credential", write)
    monkeypatch.setattr(sp, "probe_model", probe_model)
    monkeypatch.setattr(sp, "probe_all", probe_all)
    monkeypatch.setattr(paths, "configure", lambda hass: w.order.append("paths"))
    monkeypatch.setattr(nc, "configure", lambda hass: w.order.append("nc_configure"))
    monkeypatch.setattr(nc, "delete", lambda key: (w.order.append("delete"), w.deleted.append(key)))
    monkeypatch.setattr(nc, "set_many", set_many)

    hass = FakeHass()
    hass.config.path = lambda *parts: str(tmp_path.joinpath(*parts))

    class Flow(cf.NovaConfigFlow):
        configured = False
        current = ()

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create_entry", **kw}

        def async_abort(self, **kw):
            return {"type": "abort", **kw}

        async def async_set_unique_id(self, unique_id=None, **kw):
            w.order.append(("unique_id", unique_id))

        def _abort_if_unique_id_configured(self, updates=None, **kw):
            w.order.append(("check", updates))
            if self.configured:
                raise _Abort()

        def _async_current_entries(self, include_ignore=None):
            w.order.append(("entries", include_ignore))
            return list(self.current)

        async def _discover(self, provider, value):
            w.discovered.append((provider, value))
            return w.lists.get(provider)

    def make():
        flow = Flow()
        flow.hass = hass
        return flow
    w.make, w.hass = make, hass
    return w


def _blank(**values):
    base = {"groq_api_key": "", "anthropic_api_key": "", "openai_api_key": "",
            "gemini_api_key": "", "ollama_base_url": ""}
    base.update(values)
    return base


GROQ = (["openai/gpt-oss-120b", "qwen/qwen3.8-27b"], [])
OLLAMA = (["llama3.2", "llava"], [{"id": "llama3.2", "capabilities": ["completion", "tools"]},
                                  {"id": "llava", "capabilities": ["completion", "vision"]}])
ROLES = ("conversation", "classifier", "reasoning", "camera_reasoning", "vision")


# ── _find_config ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data", [
    {"api_key": "k"},
    {"groq_api_key": "k"},
    {"ollama_base_url": "http://x:11434"},
    {"custom_base_url": "http://x:8000"},
    {"llm_base_url": " http://x "},
    {"llm_provider": " Ollama "},
    {"llm_provider": "custom"},
])
def test_find_config_returns_a_file_that_names_a_usable_llm(cf, tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    assert cf._find_config(str(path)) == data


@pytest.mark.parametrize("text", [
    json.dumps({"api_key": "", "ollama_base_url": "   ", "llm_provider": "groq"}),
    json.dumps({"llm_provider": None, "custom_base_url": None}),
    "{not json",
    json.dumps(["api_key"]),
])
def test_find_config_refuses_a_file_without_a_usable_llm_or_unreadable(cf, tmp_path, text):
    path = tmp_path / "config.json"
    path.write_text(text)
    assert cf._find_config(str(path)) is None


# ── _saved_keys ──────────────────────────────────────────────────────────────

def test_saved_keys_reads_each_cloud_secret_in_order_and_strips(cf, world, load):
    hs = load("ha_secrets")
    world.saved = {"openai": "  sk-old \n", "groq": "gsk", "gemini": ""}
    assert cf._saved_keys() == {"groq": "gsk", "openai": "sk-old"}
    assert world.secret_reads == [hs.secret_key_for(f"{p}_api_key")
                                  for p in ("groq", "anthropic", "openai", "gemini")]


def test_saved_keys_skips_a_blank_secret(cf, world):
    """Fixed (bugs.md #10): a key of only spaces is not a saved key, so it is
    never tested or named as failed."""
    world.saved = {"anthropic": "   "}
    assert cf._saved_keys() == {}


async def test_a_blank_saved_key_is_not_named_as_failed(world):
    world.saved = {"anthropic": "   "}
    world.lists = {"groq": GROQ}
    flow = world.make()
    form = await flow.async_step_user(_blank(groq_api_key="gsk"))
    assert form["step_id"] == "roles" and flow._saved_failed == []
    assert world.discovered == [("groq", "gsk")]


# ── screen 1 ─────────────────────────────────────────────────────────────────

async def test_user_step_sets_paths_then_checks_the_unique_id(world):
    await world.make().async_step_user(None)
    assert world.order[:3] == ["paths", ("unique_id", "nova"), ("check", None)]
    assert "read" in world.order[3:]


async def test_user_step_form_fields_and_placeholders(world):
    world.saved = {"openai": "sk", "groq": "gsk"}
    form = await world.make().async_step_user(None)
    assert form["step_id"] == "user" and form["errors"] == {}
    password = ("TextSelector", {"type": "password"})
    assert _fields(form) == [
        ("groq_api_key", "Optional", "", None, password),
        ("anthropic_api_key", "Optional", "", None, password),
        ("openai_api_key", "Optional", "", None, password),
        ("gemini_api_key", "Optional", "", None, password),
        ("ollama_base_url", "Optional", "", None, str),
    ]
    assert form["description_placeholders"] == {"saved": "Groq, OpenAI",
                                                "example": "http://192.168.1.50:11434"}


async def test_user_step_imports_a_leftover_config_json(world):
    (world.config_dir / "nova").mkdir()
    (world.config_dir / "nova" / "config.json").write_text(json.dumps(
        {"api_key": "gsk_old", "llm_provider": "groq"}))
    res = await world.make().async_step_user(None)
    assert res["type"] == "create_entry" and res["data"]["schema_version"] == 7
    assert world.discovered == [] and "read" not in world.order


async def test_nothing_entered_and_nothing_saved_tests_nothing(world):
    res = await world.make().async_step_user(_blank())
    assert res["errors"] == {"base": "need_llm"}
    assert world.discovered == [] and world.conn_calls == []


async def test_bad_ollama_address_still_tests_the_typed_keys(world):
    world.lists = {"groq": GROQ}
    res = await world.make().async_step_user(_blank(groq_api_key="gsk",
                                                    ollama_base_url="ftp://x"))
    assert res["errors"] == {"ollama_base_url": "cannot_connect"}
    assert world.discovered == [("groq", "gsk")]
    # Neither field is blanked: the key passed and the address is not secret.
    assert _field(res, "groq_api_key")[2] == "gsk"
    assert _field(res, "ollama_base_url")[2] == "ftp://x"


async def test_only_failing_key_fields_are_blanked(world):
    world.lists = {"groq": None, "ollama": None}
    world.conn = {"groq": "authentication_failed"}
    res = await world.make().async_step_user(_blank(groq_api_key="bad",
                                                    ollama_base_url="http://x:1"))
    assert res["errors"] == {"groq_api_key": "invalid_auth",
                             "ollama_base_url": "cannot_connect"}
    assert _field(res, "groq_api_key")[2] == ""
    assert _field(res, "ollama_base_url")[2] == "http://x:1"


async def test_saved_keys_failing_are_noted_in_order_and_do_not_block(world):
    world.saved = {"openai": "sk", "groq": "gsk"}
    world.lists = {"gemini": (["gemini-x"], [])}
    world.conn = {"groq": "authentication_failed", "openai": "rate_limited"}
    flow = world.make()
    form = await flow.async_step_user(_blank(gemini_api_key="AIza"))
    assert form["step_id"] == "roles"
    assert flow._saved_failed == ["groq", "openai"]
    assert form["description_placeholders"] == {"failed": "Groq, OpenAI"}
    assert flow._entries == {"gemini": "AIza"} and flow._typed == {"gemini"}


async def test_saved_key_is_tested_before_typed_and_ollama(world):
    world.saved = {"anthropic": "sk-ant"}
    world.lists = {"anthropic": (["c"], []), "groq": GROQ, "ollama": OLLAMA}
    flow = world.make()
    await flow.async_step_user(_blank(groq_api_key="gsk", ollama_base_url="10.0.0.2"))
    assert world.discovered == [("anthropic", "sk-ant"), ("groq", "gsk"),
                                ("ollama", "http://10.0.0.2:11434")]
    assert flow._entries == {"anthropic": "sk-ant", "groq": "gsk",
                             "ollama": "http://10.0.0.2:11434"}
    assert flow._typed == {"groq"}


async def test_readable_empty_ollama_list_passes_without_a_probe(world):
    world.lists = {"ollama": ([], [])}
    flow = world.make()
    res = await flow.async_step_user(_blank(ollama_base_url="http://x:11434"))
    assert res["step_id"] == "roles"
    assert flow._lists == {"ollama": ([], [])} and world.conn_calls == []


async def test_cloud_key_without_a_list_is_probed_with_the_default_model(world, load):
    defaults = load("llm_provider").DEFAULT_MODELS
    world.lists = {"openai": None}
    world.conn = {"openai": "model_not_found"}
    flow = world.make()
    res = await flow.async_step_user(_blank(openai_api_key="sk"))
    assert res["step_id"] == "roles"
    assert world.conn_calls == [("openai", defaults["openai"], False, "sk", None)]
    assert flow._lists == {"openai": ([], [])}


async def test_access_denied_passes_a_typed_key_but_fails_a_saved_one(world):
    world.lists = {"groq": None, "openai": None}
    world.conn = {"groq": "access_denied", "openai": "access_denied"}
    world.saved = {"openai": "sk"}
    flow = world.make()
    res = await flow.async_step_user(_blank(groq_api_key="gsk"))
    assert res["step_id"] == "roles"
    assert set(flow._lists) == {"groq"} and flow._saved_failed == ["openai"]


async def test_screen_one_reads_saved_keys_on_every_submit(world):
    flow = world.make()
    await flow.async_step_user(_blank())
    await flow.async_step_user(_blank())
    assert world.order.count("read") == 8


# ── _discover ────────────────────────────────────────────────────────────────

async def test_discover_builds_the_request_and_returns_string_ids(cf, world, load, monkeypatch):
    disc = load("providers.discovery")
    seen = {}

    def resolve(config, provider):
        seen["config"], seen["provider"] = dict(config), provider
        return "request"

    async def fetch(hass, session, request):
        seen["session"], seen["request"] = session, request
        return types.SimpleNamespace(as_tuple=lambda: ([1, "b"], False, ({"id": "b"},)))

    monkeypatch.setattr(disc, "resolve_discovery_request", resolve)
    monkeypatch.setattr(disc, "fetch_models", fetch)
    monkeypatch.setattr(sys.modules["homeassistant.helpers.aiohttp_client"],
                        "async_get_clientsession", lambda hass: "session")
    flow = world.make()
    assert await cf.NovaConfigFlow._discover(flow, "anthropic", "sk") == (["1", "b"], [{"id": "b"}])
    assert seen == {"config": {"llm_provider": "anthropic", "anthropic_api_key": "sk"},
                    "provider": "anthropic", "session": "session", "request": "request"}
    await cf.NovaConfigFlow._discover(flow, "ollama", "http://x:11434")
    assert seen["config"] == {"llm_provider": "ollama", "ollama_base_url": "http://x:11434"}


async def test_discover_failure_is_none_with_a_debug_line(cf, world, load, monkeypatch, caplog):
    disc = load("providers.discovery")

    def boom(config, provider):
        raise KeyError("x")
    monkeypatch.setattr(disc, "resolve_discovery_request", boom)
    with caplog.at_level(logging.DEBUG):
        assert await cf.NovaConfigFlow._discover(world.make(), "groq", "secret-key") is None
    assert "Nova setup: groq model list unavailable (KeyError)" in caplog.text
    assert "secret-key" not in caplog.text


# ── screen 2 ─────────────────────────────────────────────────────────────────

async def test_roles_form_fields(world):
    world.lists = {"groq": GROQ, "ollama": OLLAMA}
    form = await world.make().async_step_user(_blank(groq_api_key="g", ollama_base_url="x"))
    assert "errors" not in form
    shapes = {f[0]: f for f in _fields(form)}
    assert list(shapes) == list(ROLES)
    assert shapes["conversation"] == ("conversation", "Required", None, "groq", (
        "SelectSelector", {"options": ["groq", "ollama"], "translation_key": "setup_provider",
                           "mode": "dropdown"}))
    assert shapes["vision"][3] == "groq"
    assert shapes["vision"][4][1]["options"] == ["groq", "ollama", "not_now"]


async def test_roles_submit_keeps_strings_and_blanks_missing_roles(world):
    world.lists = {"groq": GROQ}
    flow = world.make()
    await flow.async_step_user(_blank(groq_api_key="g"))
    form = await flow.async_step_roles({"conversation": "groq", "classifier": None})
    assert flow._roles == {"conversation": "groq", "classifier": "", "reasoning": "",
                           "camera_reasoning": "", "vision": ""}
    assert form["step_id"] == "models"


# ── screen 3 ─────────────────────────────────────────────────────────────────

async def _at_models(world, roles, **keys):
    flow = world.make()
    await flow.async_step_user(_blank(**keys))
    form = await flow.async_step_roles(roles)
    return flow, form


async def test_models_form_fields_and_placeholders(world):
    world.lists = {"groq": GROQ, "anthropic": None}
    roles = {"conversation": "anthropic", "classifier": "groq", "reasoning": "groq",
             "camera_reasoning": "groq", "vision": "not_now"}
    _flow, form = await _at_models(world, roles, groq_api_key="g", anthropic_api_key="a")
    assert form["errors"] == {}
    groq_select = ("SelectSelector", {"options": GROQ[0], "custom_value": True,
                                      "mode": "dropdown"})
    assert _fields(form) == [
        ("conversation_model", "Required", "claude-sonnet-5-5", None, ("TextSelector", {})),
        ("classifier_model", "Required", "openai/gpt-oss-120b", None, groq_select),
        ("reasoning_model", "Required", "openai/gpt-oss-120b", None, groq_select),
        ("camera_reasoning_model", "Required", "openai/gpt-oss-120b", None, groq_select),
    ]
    assert form["description_placeholders"] == {
        "conversation_provider": "Anthropic", "classifier_provider": "Groq",
        "reasoning_provider": "Groq", "camera_reasoning_provider": "Groq",
        "vision_provider": "not_now"}


async def test_vision_model_field_is_optional(world):
    world.lists = {"groq": GROQ}
    _flow, form = await _at_models(world, dict.fromkeys(ROLES, "groq"), groq_api_key="g")
    assert _field(form, "vision_model")[1] == "Optional"


async def test_models_required_check_strips_and_probes_nothing(world):
    world.lists = {"groq": GROQ}
    flow, _form = await _at_models(world, dict.fromkeys(ROLES, "groq"), groq_api_key="g")
    res = await flow.async_step_models({"conversation_model": "  ", "classifier_model": " m ",
                                        "reasoning_model": "m", "vision_model": ""})
    assert res["errors"] == {"conversation_model": "model_required",
                             "camera_reasoning_model": "model_required"}
    assert world.probe_calls == []
    assert _field(res, "classifier_model")[2] == "m"


async def test_models_probe_jobs_and_credentials(world, load):
    sp = load("setup_probe")
    world.lists = {"groq": GROQ, "ollama": OLLAMA}
    roles = {"conversation": "ollama", "classifier": "groq", "reasoning": "groq",
             "camera_reasoning": "groq", "vision": "ollama"}
    flow, _f = await _at_models(world, roles, groq_api_key="g", ollama_base_url="10.0.0.2")
    await flow.async_step_models({"conversation_model": "llama3.2", "classifier_model": "m",
                                  "reasoning_model": "m", "camera_reasoning_model": "m",
                                  "vision_model": "llava"})
    jobs, creds = world.probe_calls[0]
    assert jobs == {"conversation": sp.Job("ollama", "llama3.2", picture=False),
                    "classifier": sp.Job("groq", "m"), "reasoning": sp.Job("groq", "m"),
                    "camera_reasoning": sp.Job("groq", "m"),
                    "vision": sp.Job("ollama", "llava", picture=True)}
    assert creds == {"groq": ("g", None), "ollama": ("", "http://10.0.0.2:11434")}


async def test_picture_message_only_on_the_vision_field(world):
    world.lists = {"groq": GROQ}
    world.probes = {"vision": "unsupported_capability", "classifier": "unsupported_capability"}
    flow, _f = await _at_models(world, dict.fromkeys(ROLES, "groq"), groq_api_key="g")
    res = await flow.async_step_models({f"{r}_model": "m" for r in ROLES})
    assert res["errors"] == {"vision_model": "model_no_pictures",
                             "classifier_model": "test_failed"}
    assert world.written == []


async def test_keys_written_sorted_and_the_first_failure_stops_the_rest(world):
    world.lists = {"groq": GROQ, "openai": (["gpt"], [])}
    world.write_ok = lambda provider: provider != "groq"
    flow, _f = await _at_models(world, dict.fromkeys(ROLES, "groq"),
                                groq_api_key="g", openai_api_key="o")
    res = await flow.async_step_models({f"{r}_model": "m" for r in ROLES})
    assert res["errors"] == {"base": "secrets_write_failed"}
    assert world.written == [("groq", "g")] and world.set_many == []


async def test_unique_id_is_checked_again_just_before_saving(world):
    world.lists = {"groq": GROQ}
    flow, _f = await _at_models(world, dict.fromkeys(ROLES, "groq"), groq_api_key="g")
    world.order.clear()
    flow.configured = True
    with pytest.raises(_Abort):
        await flow.async_step_models({f"{r}_model": "m" for r in ROLES})
    assert world.order == [("unique_id", "nova"), ("check", None)]


async def test_success_entry_data_is_exact(world, load):
    schema = load("migrations").CURRENT_SCHEMA_VERSION
    world.lists = {"groq": GROQ, "ollama": OLLAMA}
    roles = {"conversation": "groq", "classifier": "ollama", "reasoning": "groq",
             "camera_reasoning": "groq", "vision": "not_now"}
    flow, _f = await _at_models(world, roles, groq_api_key="g", ollama_base_url="10.0.0.2")
    done = await flow.async_step_models({"conversation_model": "a", "classifier_model": "b",
                                         "reasoning_model": "c", "camera_reasoning_model": "d"})
    assert done == {"type": "create_entry", "title": "Nova", "data": {
        "honorific": "sir", "schema_version": schema, "welcome_pending": True,
        "self_hosted_endpoints_migrated": True, "ollama_base_url": "http://10.0.0.2:11434",
        "llm_provider": "groq", "model": "a", "classifier_provider": "ollama",
        "classifier_model": "b", "reasoning_provider": "groq", "reasoning_model": "c",
        "camera_reasoning_provider": "groq", "camera_reasoning_model": "d"}}
    assert world.written == [("groq", "g")]


async def test_config_json_gets_the_choices_after_the_deletions(world):
    world.lists = {"groq": GROQ}
    flow, _f = await _at_models(world, dict.fromkeys(ROLES, "groq"), groq_api_key="g")
    world.order.clear()
    await flow.async_step_models({f"{r}_model": f"m-{r}" for r in ROLES if r != "vision"}
                                 | {"vision_model": ""})
    assert world.deleted == [
        "welcome_shown", "conversation_base_url", "classifier_base_url", "reasoning_base_url",
        "suggestion_review_enabled", "suggestion_review_provider", "suggestion_review_model",
        "vision_provider", "vision_model"]
    assert world.set_many == [{
        "welcome_pending": True, "self_hosted_endpoints_migrated": True, "ollama_base_url": "",
        "llm_provider": "groq", "model": "m-conversation",
        "classifier_provider": "groq", "classifier_model": "m-classifier",
        "reasoning_provider": "groq", "reasoning_model": "m-reasoning",
        "camera_reasoning_provider": "groq", "camera_reasoning_model": "m-camera_reasoning"}]
    order = [o for o in world.order if isinstance(o, str)]
    assert order[0] == "write" and order[1] == "nc_configure" and order[-1] == "set_many"


async def test_failed_config_write_keeps_the_form(world):
    world.lists = {"groq": GROQ}
    world.config_ok = False
    flow, _f = await _at_models(world, dict.fromkeys(ROLES, "groq"), groq_api_key="g")
    res = await flow.async_step_models({f"{r}_model": "m" for r in ROLES})
    assert res["type"] == "form" and res["errors"] == {"base": "config_write_failed"}


# ── import ───────────────────────────────────────────────────────────────────

async def test_import_of_a_cloud_install(world, caplog):
    data = {"api_key": " gsk ", "llm_provider": "groq", "model": "m", "honorific": "boss",
            "groq_api_key": "x", "schema_version": 8, "observer_enabled": True}
    with caplog.at_level(logging.INFO):
        res = await world.make().async_step_import(data)
    assert res == {"type": "create_entry", "title": "Nova", "data": {
        "api_key": "gsk", "model": "m", "honorific": "boss", "llm_provider": "groq",
        "llm_base_url": "", "ollama_base_url": "", "custom_base_url": "",
        "schema_version": 7},
        "options": {"llm_provider": "groq", "model": "m", "honorific": "boss",
                    "observer_enabled": True}}
    assert world.order == [("unique_id", "nova"), ("check", {"api_key": "gsk"})]
    assert "auto-configuring from existing runtime config (provider=groq)" in caplog.text


async def test_import_falls_back_to_groq_key_and_defaults(world):
    res = await world.make().async_step_import({"groq_api_key": "gsk"})
    assert res["data"]["api_key"] == "gsk" and res["data"]["llm_provider"] == "groq"
    assert res["data"]["model"] == "openai/gpt-oss-120b" and res["data"]["honorific"] == "sir"


@pytest.mark.parametrize("provider,field,url,expect", [
    ("ollama", "ollama_base_url", "10.0.0.2", "http://10.0.0.2:11434"),
    ("custom", "custom_base_url", "http://10.0.0.3:8000/v1/", "http://10.0.0.3:8000/v1"),
])
async def test_import_of_a_local_install(world, caplog, provider, field, url, expect):
    with caplog.at_level(logging.INFO):
        res = await world.make().async_step_import({"llm_provider": provider, field: url})
    data = res["data"]
    assert data["api_key"] == "" and data["llm_base_url"] == expect
    assert data[field] == expect
    other = "custom_base_url" if provider == "ollama" else "ollama_base_url"
    assert data[other] == ""
    assert f"(provider={provider}, local)" in caplog.text


async def test_import_without_key_or_local_llm_aborts(world, caplog):
    with caplog.at_level(logging.WARNING):
        res = await world.make().async_step_import({"llm_provider": "groq"})
    assert res == {"type": "abort", "reason": "import_failed"}
    assert "Nova: config found but no API key and no local LLM" in caplog.text
    assert world.order == []


async def test_import_with_a_bad_address_keeps_a_key_with_no_address(world):
    res = await world.make().async_step_import(
        {"api_key": "k", "llm_provider": "ollama", "ollama_base_url": "ftp://x"})
    assert res["data"]["llm_base_url"] == "" and res["data"]["ollama_base_url"] == ""
    res = await world.make().async_step_import(
        {"llm_provider": "ollama", "ollama_base_url": "ftp://x"})
    assert res["reason"] == "import_failed"


async def test_import_with_a_capitalised_local_provider_is_local(world):
    """Fixed (bugs.md #7): the import lower-cases the provider too."""
    res = await world.make().async_step_import(
        {"llm_provider": "Ollama", "ollama_base_url": "http://x:11434"})
    data = res["data"]
    assert data["llm_provider"] == "ollama" and data["api_key"] == ""
    assert data["ollama_base_url"] == "http://x:11434"


@pytest.mark.parametrize("leftover", [
    {"llm_provider": "ollama"},
    {"llm_provider": "custom", "custom_base_url": "ftp://x"},
])
async def test_leftover_config_without_a_usable_address_shows_the_dialog(world, leftover):
    """Fixed (bugs.md #8): a leftover config.json that cannot be imported no
    longer blocks setup; screen 1 is shown instead."""
    (world.config_dir / "nova").mkdir()
    (world.config_dir / "nova" / "config.json").write_text(json.dumps(leftover))
    res = await world.make().async_step_user(None)
    assert res["type"] == "form" and res["step_id"] == "user"


async def test_import_with_a_null_groq_key_and_no_api_key_is_local(world):
    """Fixed (bugs.md #9): a blank Groq key no longer crashes the import."""
    res = await world.make().async_step_import(
        {"groq_api_key": None, "llm_provider": "ollama", "ollama_base_url": "http://x:1"})
    assert res["data"]["api_key"] == "" and res["data"]["ollama_base_url"] == "http://x:1"


# ── Supervisor discovery ─────────────────────────────────────────────────────

@pytest.fixture
def discovery(world, load, monkeypatch):
    store, runtime = load("voice_id_store"), load("runtime")
    d = types.SimpleNamespace(applied=[], filled={"uri": True, "token": True}, runtime=None)

    async def apply(hass, entry, uri, token):
        d.applied.append((entry, uri, token))
        return dict(d.filled)
    monkeypatch.setattr(store, "async_apply_discovery", apply)
    monkeypatch.setattr(runtime, "lifecycle_runtime", lambda entry: d.runtime)
    return d


def _info(**config):
    return types.SimpleNamespace(config=config)


@pytest.mark.parametrize("info", [_info(uri="  "), _info(uri="http://x"), types.SimpleNamespace()])
async def test_discovery_with_an_unusable_address_aborts(world, discovery, caplog, info):
    flow = world.make()
    flow.current = ("entry",)
    with caplog.at_level(logging.WARNING):
        res = await flow.async_step_hassio(info)
    assert res == {"type": "abort", "reason": "invalid_discovery_info"}
    assert "Nova: Voice ID discovery sent an unusable address" in caplog.text
    assert discovery.applied == []


async def test_discovery_without_an_entry_aborts(world, discovery):
    res = await world.make().async_step_hassio(_info(uri="tcp://a:10350"))
    assert res == {"type": "abort", "reason": "no_nova_entry"}
    assert world.order == [("entries", False)] and discovery.applied == []


async def test_discovery_saves_refreshes_and_logs_without_the_token(world, discovery, caplog):
    refreshed = []

    async def refresh():
        refreshed.append(True)
    discovery.runtime = types.SimpleNamespace(voice_id=types.SimpleNamespace(
        async_refresh_info=refresh))
    flow = world.make()
    flow.current = ("entry-1", "entry-2")
    with caplog.at_level(logging.INFO):
        res = await flow.async_step_hassio(_info(uri=" tcp://a:10350 ", token=" tok-secret "))
    assert res == {"type": "abort", "reason": "updated"}
    assert discovery.applied == [("entry-1", "tcp://a:10350", "tok-secret")]
    assert "paths" in world.order and world.order[-1] == "paths"
    await world.hass.drain()
    assert refreshed == [True]
    assert ("Nova: Voice ID app found by discovery at tcp://a:10350 "
            "(address saved, token saved)") in caplog.text
    assert "tok-secret" not in caplog.text


@pytest.mark.parametrize("token,filled,words", [
    (None, {"uri": False, "token": False}, "address kept yours, token not sent"),
    (42, {"uri": False, "token": False}, "address kept yours, token not sent"),
    ("t", {"uri": True, "token": False}, "address saved, token kept yours"),
])
async def test_discovery_log_words_and_no_refresh_without_a_new_address(
        world, discovery, caplog, token, filled, words):
    discovery.filled = filled
    discovery.runtime = types.SimpleNamespace(voice_id=None)
    flow = world.make()
    flow.current = ("entry",)
    with caplog.at_level(logging.INFO):
        await flow.async_step_hassio(_info(uri="tcp://a:10350", token=token))
    assert discovery.applied[0][2] == (token if isinstance(token, str) else None)
    assert world.hass._tasks == []
    assert words in caplog.text


def test_options_flow_factory(cf):
    entry = object()
    made = cf.NovaConfigFlow.async_get_options_flow(entry)
    assert isinstance(made, cf.NovaOptionsFlow) and made._entry is entry
    assert cf.NovaConfigFlow.VERSION == 1


# ── options flow ─────────────────────────────────────────────────────────────

@pytest.fixture
def opts(cf, load, monkeypatch):
    nc = load("nova_config")
    o = types.SimpleNamespace(config={}, saved=[], fail=None)

    def get(key, default=None):
        if o.fail:
            raise o.fail
        return o.config.get(key, default)

    def set_many(values):
        if o.fail:
            raise o.fail
        o.saved.append(values)
        return True
    monkeypatch.setattr(nc, "get", get)
    monkeypatch.setattr(nc, "set_many", set_many)

    def make(options=None, data=None):
        entry = types.SimpleNamespace(options=dict(options or {}), data=dict(data or {}))
        flow = cf.NovaOptionsFlow(entry)
        flow.hass = FakeHass()
        return flow
    o.make = make
    return o


async def test_options_menu(opts):
    res = await opts.make().async_step_init()
    assert res == {"type": "menu", "step_id": "init", "menu_options": [
        "core", "routing", "observer", "credentials", "identity", "email"]}


_SECTIONS = {
    "core": [
        ("honorific", "sir", ("SelectSelector", {"options": ["sir", "ma'am", "boss", "friend"],
                                                 "custom_value": True, "mode": "dropdown"})),
        ("directive_preset", "guardian_steward", "PRESETS"),
        ("directive", "", ("TextSelector", {"multiline": True})),
        ("model", "openai/gpt-oss-120b", ("TextSelector", {})),
        ("use_hass_api", True, ("BooleanSelector", {})),
    ],
    "routing": [
        ("bedroom_areas", [], ("AreaSelector", {"multiple": True})),
        ("ground_floor_areas", [], ("AreaSelector", {"multiple": True})),
        ("broadcast_group", "", ("EntitySelector", {"domain": "media_player"})),
        ("notify_service", "", ("TextSelector", {})),
    ],
    "observer": [
        ("observer_enabled", False, ("BooleanSelector", {})),
        ("classifier_model", "openai/gpt-oss-120b", ("TextSelector", {})),
        ("reasoning_model", "openai/gpt-oss-120b", ("TextSelector", {})),
        ("review_model", "openai/gpt-oss-120b", ("TextSelector", {})),
        ("observer_quiet_start", "22:00", ("TextSelector", {})),
        ("observer_quiet_end", "07:00", ("TextSelector", {})),
    ],
    "identity": [
        ("identity_enabled", True, ("BooleanSelector", {})),
        ("identity_voice_fingerprint", False, ("BooleanSelector", {})),
        ("voice_recognition_source", "", ("TextSelector", {})),
        ("voice_recognition_auto_enroll", True, ("BooleanSelector", {})),
        ("identity_min_confidence", 0.45, ("NumberSelector", {
            "min": 0.0, "max": 1.0, "step": 0.05, "mode": "slider"})),
    ],
    "email": [
        ("imap_enabled", False, ("BooleanSelector", {})),
        ("imap_host", "", ("TextSelector", {})),
        ("imap_port", 993, ("NumberSelector", {"min": 1, "max": 65535, "step": 1,
                                               "mode": "box"})),
        ("imap_user", "", ("TextSelector", {})),
        ("imap_folder", "INBOX", ("TextSelector", {})),
        ("imap_ssl", True, ("BooleanSelector", {})),
        ("imap_secret_key", "nova_imap_password", ("TextSelector", {})),
    ],
}


@pytest.mark.parametrize("section", sorted(_SECTIONS))
async def test_options_section_fields_and_defaults(opts, load, section):
    presets = list(load("const").DIRECTIVE_PRESETS.keys())
    form = await getattr(opts.make(), f"async_step_{section}")(None)
    assert form["type"] == "form" and form["step_id"] == section and "errors" not in form
    expected = [(key, "Optional", default, None,
                 ("SelectSelector", {"options": presets, "mode": "dropdown"})
                 if widget == "PRESETS" else widget)
                for key, default, widget in _SECTIONS[section]]
    assert _fields(form) == expected


async def test_current_value_prefers_runtime_config_then_options_then_data(opts):
    opts.config = {"honorific": "boss", "model": ""}
    flow = opts.make(options={"model": "opt-model"}, data={"model": "data-model",
                                                           "directive": "data-d"})
    form = await flow.async_step_core(None)
    suggested = {f[0]: f[2] for f in _fields(form)}
    assert suggested["honorific"] == "boss"
    assert suggested["model"] == "opt-model"
    assert suggested["directive"] == "data-d"


async def test_current_value_survives_a_failing_runtime_config(opts):
    opts.fail = RuntimeError("down")
    flow = opts.make(data={"imap_port": 143})
    form = await flow.async_step_email(None)
    assert _field(form, "imap_port")[2] == 143


async def test_runtime_config_falsy_values_other_than_blank_win(opts):
    opts.config = {"observer_enabled": False, "bedroom_areas": []}
    flow = opts.make(options={"observer_enabled": True, "bedroom_areas": ["area_one"]})
    assert _field(await flow.async_step_observer(None), "observer_enabled")[2] is False
    assert _field(await flow.async_step_routing(None), "bedroom_areas")[2] == []


@pytest.mark.parametrize("section", ["core", "routing", "observer", "identity", "email"])
async def test_section_submit_saves_to_runtime_config_and_entry(opts, section):
    flow = opts.make(options={"kept": 1, "imap_host": "old"})
    submitted = {"imap_host": "mail.example", "x": 2}
    res = await getattr(flow, f"async_step_{section}")(submitted)
    assert res == {"type": "create_entry", "title": "",
                   "data": {"kept": 1, "imap_host": "mail.example", "x": 2}}
    assert opts.saved == [submitted] and opts.saved[0] is not submitted
    assert flow._data == submitted


async def test_section_submit_still_finishes_when_the_write_fails(opts, caplog):
    opts.fail = OSError("disk full")
    with caplog.at_level(logging.WARNING):
        res = await opts.make().async_step_routing({"notify_service": "notify.one"})
    assert res["data"] == {"notify_service": "notify.one"}
    assert "Nova options: nova_config write failed: disk full" in caplog.text


# ── options: credentials ─────────────────────────────────────────────────────

_ALL = ("groq", "openai", "anthropic", "gemini", "custom", "ollama")


@pytest.fixture
def creds(opts, load, monkeypatch):
    hs = load("ha_secrets")
    c = types.SimpleNamespace(has={"gemini"}, ops=[], status_reads=[])

    def has(provider, path=None):
        c.status_reads.append(provider)
        return provider in c.has

    async def put(hass, provider, value):
        c.ops.append(("set", provider, value))
        return False

    async def drop(hass, provider):
        c.ops.append(("delete", provider))
        return False
    monkeypatch.setattr(hs, "has_provider_credential_sync", has)
    monkeypatch.setattr(hs, "async_set_provider_credential", put)
    monkeypatch.setattr(hs, "async_delete_provider_credential", drop)
    return c


async def test_credentials_form(opts, creds):
    form = await opts.make(data={"llm_provider": " OpenAI "}).async_step_credentials(None)
    expected = []
    for p in _ALL:
        expected += [(f"{p}_credential", "Optional", None, "",
                      ("TextSelector", {"type": "password"})),
                     (f"clear_{p}", "Optional", None, False, ("BooleanSelector", {}))]
    expected.append(("confirm_delete_active", "Optional", None, False, ("BooleanSelector", {})))
    assert _fields(form) == expected
    assert form["errors"] == {}
    assert form["description_placeholders"] == {
        "status": "groq ✗, openai ✗, anthropic ✗, gemini ✓, custom ✗, ollama ✗",
        "active": "openai"}
    assert creds.status_reads == list(_ALL)


async def test_credentials_active_provider_defaults_to_groq(opts, creds):
    opts.config = {"llm_provider": ""}
    form = await opts.make(options={"llm_provider": None}).async_step_credentials(None)
    assert form["description_placeholders"]["active"] == "groq"


async def test_credentials_writes_then_deletes_and_keeps_the_options(opts, creds):
    flow = opts.make(options={"a": 1})
    res = await flow.async_step_credentials({"ollama_credential": " tok ", "clear_groq": True,
                                             "clear_openai": True, "confirm_delete_active": True})
    assert res == {"type": "create_entry", "title": "", "data": {"a": 1}}
    assert creds.ops == [("set", "ollama", "tok"), ("delete", "groq"), ("delete", "openai")]
    assert opts.saved == []


async def test_credentials_set_and_clear_stops_at_that_provider(opts, creds):
    res = await opts.make().async_step_credentials({
        "groq_credential": "g", "openai_credential": "o", "clear_openai": True,
        "clear_gemini": True, "gemini_credential": "x"})
    assert res["type"] == "form"
    assert res["errors"] == {"base": "credential_set_and_clear"}
    assert creds.ops == []


async def test_credentials_clearing_the_active_provider_needs_the_box(opts, creds):
    flow = opts.make(options={"llm_provider": "anthropic"})
    res = await flow.async_step_credentials({"clear_anthropic": True})
    assert res["errors"] == {"base": "confirm_required_for_active_provider"}
    assert res["description_placeholders"]["active"] == "anthropic"
    assert creds.ops == []
