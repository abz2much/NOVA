"""Diagnostics download: the error lines, the log tails and the service health
section are scrubbed too (v8.7.5). Each must lose a URL's user:pass@, a
?token= query value and a webhook address's path, whatever produced it."""
import importlib.util
import json
import pathlib
import sys
import types

import pytest

_COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"

_CRED_URL = "http://nova:pa55word@10.0.0.5:11434/api/chat"
_TOKEN_URL = "https://search.example/?q=x&token=abc123"
_HOOK_URL = "https://n8n.example/webhook/0f3c9e7a"
_SECRETS = ("pa55word", "abc123", "0f3c9e7a")
_LEAKY = f"failed at {_CRED_URL} then {_TOKEN_URL} and {_HOOK_URL}"


@pytest.fixture
def diag():
    key = "jc.diagnostics"
    if key in sys.modules:
        return sys.modules[key]
    init = _COMP / "diagnostics" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        key, init, submodule_search_locations=[str(_COMP / "diagnostics")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _assert_scrubbed(text):
    for secret in _SECRETS:
        assert secret not in text, f"{secret!r} survived: {text}"
    assert "**REDACTED**" in text


def _boom(*_a, **_k):
    raise RuntimeError(_LEAKY)


def _module(name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class _Entry:
    version = 1

    @property
    def data(self):
        _boom()


class _FailingStates:
    def async_all(self):
        _boom()

    def get(self, _entity_id):
        return None


class _Hass:
    def __init__(self, states=None):
        self.states = states or _FailingStates()

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


class _Core:
    def status(self):
        _boom()


async def _boom_async(_hass):
    _boom()


@pytest.fixture
async def failing_dump(diag, monkeypatch):
    """The dump when every source raises an exception carrying credentials."""
    ws = sys.modules["jc.websocket"]
    monkeypatch.setattr(ws, "recent_debug_log", _boom, raising=False)
    monkeypatch.setattr(ws, "recent_conversation_log", _boom, raising=False)
    monkeypatch.setitem(sys.modules, "jc.nova_config", _module("nova_config", get_all=_boom))
    monkeypatch.setitem(sys.modules, "jc.cognitive_core", _module("cognitive_core", _CORE=_Core()))
    monkeypatch.setitem(sys.modules, "jc.connectivity", _module("connectivity", snapshot=_boom))
    for name, fn in (("cognition", "stats"), ("decision_record", "stats"),
                     ("intrusion", "status"), ("reasoning_cache", "stats")):
        monkeypatch.setitem(sys.modules, f"jc.{name}", _module(name, **{fn: _boom}))
    monkeypatch.setattr(diag, "run_service_health", _boom_async)
    return await diag.async_get_config_entry_diagnostics(_Hass(), _Entry())


@pytest.mark.parametrize("field", [
    "config_error", "nova_config_error", "service_health_error", "cognitive_error",
    "connectivity_error", "entity_count_error", "audio_routing_error",
    "recent_log_error", "conversation_log_error",
])
async def test_error_fields_are_scrubbed(failing_dump, field):
    text = failing_dump[field]
    assert text.startswith("RuntimeError: failed at ")
    assert len(text) <= 200
    _assert_scrubbed(text)
    # the non secret parts of each address stay, so the error is still useful
    assert "10.0.0.5:11434" in text and "n8n.example/webhook/" in text


@pytest.mark.parametrize("name", ["cognition", "decision_record", "intrusion", "reasoning_cache"])
async def test_subsystem_error_fallback_is_scrubbed(failing_dump, name):
    entry = failing_dump["subsystems"][name]
    assert list(entry) == ["error"]
    assert entry["error"].startswith("RuntimeError: ")
    _assert_scrubbed(entry["error"])


async def test_whole_failing_dump_holds_no_secret(failing_dump):
    dumped = json.dumps(failing_dump)
    for secret in _SECRETS:
        assert secret not in dumped


def test_err_truncates_and_keeps_the_type(diag):
    text = diag._err(ValueError("x" * 500 + _CRED_URL))
    assert text.startswith("ValueError: ")
    assert len(text) == 200


# ── Log tails and service health ────────────────────────────────────────────

def _log_entries():
    return [
        {"date": "2026-10-04", "ts": "18:00:01", "cat": "reply",
         "msg": "Reply routed to the paired speaker"},
        {"date": "2026-10-04", "ts": "18:00:02", "cat": "error",
         "msg": f"LLM call {_LEAKY}"},
    ]


@pytest.fixture
async def healthy_dump(diag, monkeypatch):
    ws = sys.modules["jc.websocket"]
    monkeypatch.setattr(ws, "recent_debug_log", lambda n=150: _log_entries(), raising=False)
    monkeypatch.setattr(ws, "recent_conversation_log", lambda n=80: _log_entries(), raising=False)

    async def _health(_hass):
        return {"overall": "down", "summary": f"LLM down: {_HOOK_URL}", "services": [
            {"name": "LLM", "key": "llm", "status": "down",
             "detail": f"unreachable {_CRED_URL}", "fail_detail": f"timeout {_TOKEN_URL}"},
            {"name": "TTS", "key": "tts", "status": "down", "error": _LEAKY},
        ]}
    monkeypatch.setattr(diag, "run_service_health", _health)

    class _States:
        def async_all(self):
            return []

        def get(self, _entity_id):
            return None
    return await diag.async_get_config_entry_diagnostics(_Hass(_States()), _Entry())


@pytest.mark.parametrize("key", ["recent_log", "conversation_log"])
async def test_log_tails_are_scrubbed(healthy_dump, key):
    _assert_scrubbed(healthy_dump[key][1]["msg"])


@pytest.mark.parametrize("key", ["recent_log", "conversation_log"])
async def test_log_entries_keep_their_shape_and_plain_text(healthy_dump, key):
    out = healthy_dump[key]
    src = _log_entries()
    assert [list(e) for e in out] == [list(e) for e in src]
    assert out[0] == src[0]
    for k in ("date", "ts", "cat"):
        assert out[1][k] == src[1][k]
    assert out[1]["msg"].startswith("LLM call failed at http://**REDACTED**@10.0.0.5:11434")


async def test_service_health_is_scrubbed(healthy_dump):
    health = healthy_dump["service_health"]
    assert set(health) == {"overall", "summary", "services"}
    assert health["overall"] == "down"
    llm, tts = health["services"]
    assert (llm["name"], llm["key"], llm["status"]) == ("LLM", "llm", "down")
    _assert_scrubbed(health["summary"])
    _assert_scrubbed(llm["detail"])
    _assert_scrubbed(llm["fail_detail"])
    _assert_scrubbed(tts["error"])


def test_scrub_text_hides_webhook_paths(diag):
    for raw, want in (
        ("https://n8n.example/webhook/0f3c", "https://n8n.example/webhook/**REDACTED**"),
        ("https://n8n.example/webhook-test/0f3c", "https://n8n.example/webhook-test/**REDACTED**"),
        ("https://ha.example/api/webhook/abc-1", "https://ha.example/api/webhook/**REDACTED**"),
        ("https://discord.com/api/webhooks/1/tok", "https://discord.com/api/webhooks/**REDACTED**"),
        ("https://hooks.slack.com/services/T0/B0/x", "https://hooks.slack.com/services/**REDACTED**"),
    ):
        assert diag._scrub_text(f"POST {raw} failed") == f"POST {want} failed"
    assert diag._scrub_text("the webhook trigger fired") == "the webhook trigger fired"
