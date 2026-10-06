"""Pin what the AI settings commands in ws_ai.py do today (8.7.22, tests only).

nova/list_models, nova/test_provider_endpoint, nova/apply_ai_config,
nova/get_credential_status, nova/set_credential and nova/delete_credential.
test_ai_config_apply.py already checks the two validators as extracted
functions; here the handlers themselves run, with the decorators stubbed to
pass through, a recording connection, and fakes for the network, the config
store and the secrets store. Tests named test_current_behaviour_* pin
behaviour that looks wrong; each says why.
"""
from __future__ import annotations

import sys
import types

import pytest

from test_pin_ws_update_config import _Conn, _entry, _hass, _stub_ws_api

_ROLES = ("llm_provider", "model", "classifier_provider", "classifier_model",
          "reasoning_provider", "reasoning_model", "vision_provider", "vision_model",
          "camera_reasoning_provider", "camera_reasoning_model")


@pytest.fixture
def ai(load, monkeypatch):
    # ws_bridge looks _get_entry up on jc.websocket at call time, so that
    # module is loaded too, under the same pass-through decorators.
    _stub_ws_api(monkeypatch)
    for name in ("jc.ws_ai", "jc.websocket"):
        sys.modules.pop(name, None)
    load("websocket")
    mod = load("ws_ai")
    yield mod
    for name in ("jc.ws_ai", "jc.websocket"):
        sys.modules.pop(name, None)


@pytest.fixture
def cfg(load, monkeypatch):
    """The saved config every handler reads, and what apply saves."""
    nc = load("nova_config")
    llm = load("llm_provider")
    state = types.SimpleNamespace(
        config={k: ("groq" if k.endswith("provider") else "model-1") for k in _ROLES},
        creds={"groq"}, saved=[], save_ok=True, tests=[], test_problem=None)

    def effective_with_runtime(entry, runtime=None):
        out = dict(state.config)
        out.update(runtime or {})
        return out

    def set_many_atomic(updates):
        state.saved.append(dict(updates))
        return state.save_ok

    async def test_connection(hass, provider, key, model, endpoint):
        state.tests.append((provider, model, endpoint))
        return state.test_problem
    monkeypatch.setattr(nc, "effective_config_with_runtime", effective_with_runtime)
    monkeypatch.setattr(nc, "effective_config", lambda entry=None: dict(state.config))
    monkeypatch.setattr(nc, "set_many_atomic", set_many_atomic)
    monkeypatch.setattr(llm, "resolve_provider_credential",
                        lambda c, p: "key" if p in state.creds else "")
    monkeypatch.setattr(llm, "test_connection", test_connection)
    return state


@pytest.fixture
def dropped(ai, monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "invalidate_model_cache", lambda p=None: seen.append(p))
    return seen


def _reloading_hass(entry):
    hass = _hass(entry)
    reloads = []

    async def async_reload(entry_id):
        reloads.append(entry_id)
    hass.config_entries.async_reload = async_reload
    hass.reloads = reloads
    return hass


async def _apply(ai, hass, updates):
    conn = _Conn()
    await ai.ws_apply_ai_config(hass, conn, {"id": 3, "updates": updates})
    (msg_id, res), = conn.results
    assert conn.errors == [] and msg_id == 3
    return res


# ── nova/apply_ai_config: refused before anything is saved ────────────────

@pytest.mark.parametrize("updates,message", [
    ({}, "No AI settings were supplied"),
    ({"api_key": "sk-x"}, "The request contains unsupported AI settings"),
    ({"llm_base_url": "http://x"}, "The request contains unsupported AI settings"),
    ({"llm_provider": "bogus"}, "Unsupported provider for llm_provider"),
    ({"model": "  "}, "A valid model is required for model"),
    ({"model": "m" * 513}, "A valid model is required for model"),
    ({"ollama_num_ctx": True}, "Ollama context length must be a number"),
    ({"ollama_num_ctx": "lots"}, "Ollama context length must be a number"),
    ({"ollama_num_ctx": 511}, "Ollama context length must be between 512 and 262144"),
    ({"home_context_max_entities": False}, "Prompt size must be a number"),
    ({"home_context_max_entities": None}, "Prompt size must be a number"),
    ({"home_context_max_entities": 51}, "Prompt size must be between 0 and 50"),
])
async def test_a_bad_update_is_refused_with_its_reason_and_nothing_is_saved(
        ai, load, cfg, dropped, updates, message):
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, updates)
    assert res == {"ok": False, "error": "invalid_configuration", "message": message}
    assert cfg.saved == [] and hass.reloads == [] and dropped == []


_CREDS = ("The endpoint must not include a user name or password. "
          "Add the key under Provider Credentials instead.")


@pytest.mark.parametrize("endpoint,message", [
    # 8.7.23: was test_current_behaviour_a_bad_endpoint_is_refused_without_
    # its_reason, which got "ValueError (details are in the Home Assistant
    # log)". Each reason is now a fixed phrase; no exception text is sent.
    ("http://user:pw@10.0.0.2:11434", _CREDS),
    ("ftp://10.0.0.2", "The endpoint must start with http:// or https://."),
    ("http://", "The endpoint must include a host name or address."),
    ("http://10.0.0.2/?token=abc", "The endpoint must not include a query string (?…) "
                                   "or a fragment (#…)."),
    ("http://10.0.0.2:99999", "The endpoint has an invalid port."),
    ("http://" + "a" * 2050, "The endpoint is too long."),
])
async def test_a_bad_endpoint_is_refused_with_a_fixed_reason(ai, load, cfg, endpoint, message):
    res = await _apply(ai, _reloading_hass(_entry(load)), {"ollama_base_url": endpoint})
    assert res == {"ok": False, "error": "invalid_configuration", "message": message}
    assert cfg.saved == []
    assert "user:pw" not in res["message"] and "token" not in res["message"]


def test_every_fixed_reason_matches_a_real_normaliser_refusal(ai, load):
    # If providers.routing rewords a reason, the table would silently fall
    # back to the generic phrase; this keeps them in step.
    routing = load("providers.routing")
    raised = set()
    for bad in ("http://user:pw@h", "ftp://h", "http://", "http://h/?q=1", "http://h:99999",
                "http://" + "a" * 2050):
        with pytest.raises(ValueError) as info:
            routing.normalize_provider_endpoint(bad, "ollama")
        raised.add(info.value.args)
    assert raised == {(k,) for k in ai._ENDPOINT_REASONS}


def test_an_unknown_value_error_gets_the_fixed_fallback(ai, caplog):
    msg = ai._value_error_message(ValueError("socket /run/secret token=abc"))
    assert msg == "The endpoint is not a valid address."
    assert "AI settings failed" in caplog.text


def test_novas_own_validation_message_is_kept(ai, load):
    err = load("safe_errors").NovaValidationError("Prompt size must be a number")
    assert ai._value_error_message(err) == "Prompt size must be a number"


async def test_no_entry_is_reported(ai, load, cfg):
    hass = _hass(None)
    res = await _apply(ai, hass, {"model": "model-2"})
    assert res == {"ok": False, "error": "no_entry", "message": "Nova is not loaded."}
    assert cfg.saved == []


async def test_an_entry_with_no_runtime_saves_nothing(ai, load, cfg):
    res = await _apply(ai, _reloading_hass(_entry(load, with_runtime=False)), {"model": "m2"})
    assert res == {"ok": False, "error": "apply_failed",
                   "message": "Nova could not apply the AI settings."}
    assert cfg.saved == [] and cfg.tests == []


async def test_a_cloud_provider_without_its_key_is_refused(ai, load, cfg):
    res = await _apply(ai, _reloading_hass(_entry(load)), {"llm_provider": "openai"})
    assert res["error"] == "invalid_configuration"
    assert res["message"] == "Add the openai credential before applying"
    assert cfg.saved == []


async def test_a_self_hosted_role_needs_its_endpoint(ai, load, cfg):
    res = await _apply(ai, _reloading_hass(_entry(load)),
                       {"vision_provider": "ollama", "vision_model": "llava"})
    assert res["message"] == "Set the ollama endpoint before applying"
    assert res["errors"] == ["Set the ollama endpoint before applying"]
    assert cfg.saved == [] and cfg.tests == []


async def test_a_failed_connection_test_saves_nothing(ai, load, cfg, dropped):
    cfg.test_problem = "cannot_connect"
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, {"vision_provider": "ollama", "vision_model": "llava",
                                  "ollama_base_url": "10.0.0.2"})
    assert res == {"ok": False, "error": "connection_test_failed",
                   "message": "Vision could not use llava on ollama."}
    assert cfg.tests == [("ollama", "llava", "http://10.0.0.2:11434")]
    assert cfg.saved == [] and hass.reloads == [] and dropped == []


async def test_each_self_hosted_model_is_tested_once(ai, load, cfg):
    updates = {"vision_provider": "ollama", "vision_model": "llava",
               "camera_reasoning_provider": "ollama", "camera_reasoning_model": "llava",
               "ollama_base_url": "10.0.0.2"}
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, updates)
    assert res["ok"] is True
    assert cfg.tests == [("ollama", "llava", "http://10.0.0.2:11434")]
    await hass.drain()


async def test_a_failed_save_is_reported_and_nothing_goes_live(ai, load, cfg, dropped):
    cfg.save_ok = False
    entry = _entry(load, runtime_config={"model": "old"})
    hass = _reloading_hass(entry)
    res = await _apply(ai, hass, {"model": "model-2"})
    assert res == {"ok": False, "error": "persist_failed",
                   "message": "Nova could not save the AI settings."}
    assert entry.runtime_data.runtime_config == {"model": "old"}
    assert hass.reloads == [] and dropped == []


async def test_a_good_apply_saves_goes_live_and_reloads(ai, load, cfg, dropped):
    entry = _entry(load)
    hass = _reloading_hass(entry)
    res = await _apply(ai, hass, {"model": " model-2 ", "llm_provider": "GROQ"})
    assert res == {"ok": True, "message": "AI settings saved. Nova is reloading."}
    want = {"model": "model-2", "llm_provider": "groq", "llm_base_url": "",
            "self_hosted_endpoints_migrated": True}
    assert cfg.saved == [want]
    assert entry.runtime_data.runtime_config == want
    assert dropped == [None]
    await hass.drain()
    assert hass.reloads == ["e1"]


async def test_a_save_that_raises_returns_no_raw_text(ai, load, cfg, monkeypatch):
    def boom(updates):
        raise OSError("/config/nova/config.json: token=abc")
    monkeypatch.setattr(load("nova_config"), "set_many_atomic", boom)
    res = await _apply(ai, _reloading_hass(_entry(load)), {"model": "m2"})
    assert res == {"ok": False, "error": "apply_failed",
                   "message": "Nova could not apply the AI settings."}


# ── nova/test_provider_endpoint ─────────────────────────────────────────────

@pytest.fixture
def net(ai, load, monkeypatch):
    """The destination check and the model fetch."""
    dest = load("providers.destinations")
    st = types.SimpleNamespace(checked=[], refuse=False, fetched=[], fetch_error=None,
                               models=(["llama3"], False, [{"id": "llama3"}]))
    errors = load("providers.errors")

    def check_url(url, resolve=False):
        st.checked.append((url, resolve))
        if st.refuse:
            raise errors.ProviderError(errors.ProviderErrorKind.INVALID_ENDPOINT, "ollama")

    async def fetch(hass, provider, config):
        st.fetched.append((provider, dict(config)))
        if st.fetch_error is not None:
            raise st.fetch_error
        return st.models
    monkeypatch.setattr(dest, "check_url", check_url)
    monkeypatch.setattr(ai, "_fetch_models", fetch)
    st.errors = errors
    return st


async def _test_endpoint(ai, hass, provider, endpoint):
    conn = _Conn()
    await ai.ws_test_provider_endpoint(hass, conn, {"id": 4, "provider": provider,
                                                    "endpoint": endpoint})
    assert conn.errors == []
    return conn.results[0][1]


async def test_a_staged_endpoint_is_checked_tested_and_not_saved(ai, load, cfg, net):
    res = await _test_endpoint(ai, _hass(_entry(load)), "ollama", "10.0.0.2")
    assert res == {"ok": True, "provider": "ollama", "endpoint": "http://10.0.0.2:11434",
                   "models": ["llama3"], "model_details": [{"id": "llama3"}],
                   "truncated": False}
    assert net.checked == [("http://10.0.0.2:11434", True)]
    assert net.fetched[0][1]["ollama_base_url"] == "http://10.0.0.2:11434"
    assert cfg.saved == []


async def test_an_empty_endpoint_is_refused_with_its_reason(ai, load, cfg, net):
    res = await _test_endpoint(ai, _hass(_entry(load)), "custom", "   ")
    assert res == {"ok": False, "error": "invalid_endpoint", "message": "Endpoint is required"}
    assert net.checked == [] and net.fetched == []


async def test_a_refused_destination_is_never_fetched(ai, load, cfg, net):
    net.refuse = True
    res = await _test_endpoint(ai, _hass(_entry(load)), "custom", "http://169.254.169.254")
    assert res == {"ok": False, "error": "invalid_endpoint",
                   "message": "Endpoint is not an allowed destination"}
    assert net.fetched == []


async def test_a_redirect_to_a_refused_destination_is_reported_as_invalid(ai, load, cfg, net):
    net.fetch_error = net.errors.ProviderError(net.errors.ProviderErrorKind.INVALID_ENDPOINT,
                                               "custom")
    res = await _test_endpoint(ai, _hass(_entry(load)), "custom", "http://10.0.0.3:8080")
    assert res == {"ok": False, "error": "invalid_endpoint",
                   "message": "Endpoint is not an allowed destination"}


async def test_any_other_fetch_failure_returns_a_fixed_message(ai, load, cfg, net):
    net.fetch_error = RuntimeError("401 from http://user:pw@10.0.0.3 token=abc")
    res = await _test_endpoint(ai, _hass(_entry(load)), "custom", "http://10.0.0.3:8080")
    assert res == {"ok": False, "error": "model_discovery_unavailable",
                   "message": "Could not connect to the endpoint or list its models."}


async def test_a_malformed_staged_endpoint_is_refused_with_its_reason(ai, load, cfg, net):
    # 8.7.23: was test_current_behaviour_a_malformed_endpoint_is_refused_
    # without_its_reason. Same fixed phrases as apply.
    res = await _test_endpoint(ai, _hass(_entry(load)), "ollama", "http://u:p@10.0.0.2")
    assert res == {"ok": False, "error": "invalid_endpoint", "message": _CREDS}
    assert net.fetched == [] and net.checked == []


# ── nova/list_models ────────────────────────────────────────────────────────

@pytest.fixture
def cache(load, monkeypatch):
    disc = load("providers.discovery")
    fresh = disc.ModelListCache()
    monkeypatch.setattr(disc, "MODEL_CACHE", fresh)
    return fresh


async def _list(ai, hass, provider, refresh=False):
    conn = _Conn()
    await ai.ws_list_models(hass, conn, {"id": 5, "provider": provider, "refresh": refresh})
    assert conn.errors == []
    return conn.results[0][1]


async def test_list_models_fetches_then_serves_from_the_cache(ai, load, cfg, net, cache,
                                                              monkeypatch):
    async def deduped(hass, provider, config, key):
        net.fetched.append(key)
        return net.models
    monkeypatch.setattr(ai, "_fetch_models_deduped", deduped)
    hass = _hass(_entry(load))
    first = await _list(ai, hass, "Groq")
    assert first == {"provider": "groq", "models": ["llama3"], "model_details": [{"id": "llama3"}],
                     "cached": False, "truncated": False}
    second = await _list(ai, hass, "groq")
    assert second["cached"] is True and second["models"] == ["llama3"]
    third = await _list(ai, hass, "groq", refresh=True)
    assert third["cached"] is False
    assert len(net.fetched) == 2


async def test_list_models_failure_is_a_result_with_a_fixed_error(ai, load, cfg, cache,
                                                                   monkeypatch):
    async def deduped(*a):
        raise RuntimeError("token=abc")
    monkeypatch.setattr(ai, "_fetch_models_deduped", deduped)
    res = await _list(ai, _hass(_entry(load)), "groq")
    assert res == {"provider": "groq", "models": [], "error": "model_discovery_unavailable"}


async def test_list_models_for_an_unknown_provider_is_the_same_fixed_error(ai, load, cfg, cache):
    res = await _list(ai, _hass(_entry(load)), "nope")
    assert res == {"provider": "nope", "models": [], "error": "model_discovery_unavailable"}


# ── credentials ─────────────────────────────────────────────────────────────

@pytest.fixture
def secrets(load, monkeypatch):
    hs = load("ha_secrets")
    st = types.SimpleNamespace(ok=True, raise_=False, calls=[])

    async def set_cred(hass, provider, value):
        st.calls.append(("set", provider, value))
        if st.raise_:
            raise RuntimeError(f"could not write secrets.yaml with {value}")
        return st.ok

    async def delete_cred(hass, provider):
        st.calls.append(("delete", provider))
        if st.raise_:
            raise RuntimeError("secrets.yaml locked")
        return st.ok

    async def status(hass):
        return {"groq": True, "openai": False}
    monkeypatch.setattr(hs, "async_set_provider_credential", set_cred)
    monkeypatch.setattr(hs, "async_delete_provider_credential", delete_cred)
    monkeypatch.setattr(hs, "async_credential_status", status)
    return st


async def _cred(handler, hass, **msg):
    conn = _Conn()
    await handler(hass, conn, {"id": 6, **msg})
    assert conn.errors == []
    return conn.results[0][1]


async def test_setting_a_credential_never_echoes_it(ai, load, secrets, dropped):
    res = await _cred(ai.ws_set_credential, _hass(_entry(load)), provider=" OpenAI ",
                      value="sk-secret")
    assert res == {"ok": True}
    assert secrets.calls == [("set", " OpenAI ", "sk-secret")]
    assert dropped == ["openai"]


async def test_a_refused_or_failed_credential_write_is_ok_false(ai, load, secrets, dropped):
    secrets.ok = False
    assert await _cred(ai.ws_set_credential, _hass(_entry(load)), provider="groq",
                       value="") == {"ok": False}
    secrets.raise_ = True
    assert await _cred(ai.ws_set_credential, _hass(_entry(load)), provider="groq",
                       value="sk-secret") == {"ok": False}
    assert dropped == []


async def test_deleting_a_credential(ai, load, secrets, dropped):
    assert await _cred(ai.ws_delete_credential, _hass(_entry(load)),
                       provider="Groq") == {"ok": True}
    assert dropped == ["groq"]
    secrets.ok = False
    assert await _cred(ai.ws_delete_credential, _hass(_entry(load)),
                       provider="groq") == {"ok": False}
    secrets.raise_ = True
    assert await _cred(ai.ws_delete_credential, _hass(_entry(load)),
                       provider="groq") == {"ok": False}
    assert dropped == ["groq"]


async def test_credential_status_is_booleans_only(ai, load, cfg, secrets):
    cfg.config["ollama_base_url"] = "http://10.0.0.2:11434"
    res = await _cred(ai.ws_get_credential_status, _hass(_entry(load)))
    assert res["status"] == {"groq": True, "openai": False}
    assert res["available"] == {"groq": True, "openai": False, "anthropic": False,
                                "gemini": False, "custom": False, "ollama": True}


async def test_credential_status_failure_is_empty(ai, load, cfg, monkeypatch):
    async def boom(hass):
        raise RuntimeError("x")
    monkeypatch.setattr(load("ha_secrets"), "async_credential_status", boom)
    assert await _cred(ai.ws_get_credential_status, _hass(_entry(load))) == {
        "status": {}, "available": {}}
