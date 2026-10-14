"""Core service health, checked against its spec (7e).

Ten checks (LLM, embeddings, TTS, STT, Voice ID, cameras, routines, the
conversation store, the scheduler and host health) each give one row, and the
roll-up turns them into an overall state and a one-line summary. Only a real
failure during use may read DOWN; a missed health poke is IDLE at worst.
Everything the checks reach for (settings, the breaker, the endpoint resolver,
the embeddings probe, HTTP, the runtime, the store, host health) is faked.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def sh(load, monkeypatch):
    m = load("diagnostics.service_health")
    monkeypatch.setattr(m, "_USAGE", {})
    settings: dict = {}
    monkeypatch.setattr(m, "_cfg", lambda key, default=None: settings.get(key, default))
    m_ns = types.SimpleNamespace(mod=m, settings=settings)
    return m_ns


def _fake(monkeypatch, name, **attrs):
    """Install jc.<name> in sys.modules and on the jc package (a plain
    `from .. import X` reads the package attribute first)."""
    mod = types.ModuleType(f"jc.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    monkeypatch.setattr(sys.modules["jc"], name, mod, raising=False)
    return mod


@pytest.fixture
def no_sleep(sh, monkeypatch):
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(sh.mod.asyncio, "sleep", _sleep)
    return slept


class _Resp:
    def __init__(self, status, payload=None, bad_json=False):
        self.status = status
        self._payload = payload
        self._bad = bad_json

    async def json(self, content_type="x"):
        assert content_type is None
        if self._bad:
            raise ValueError("not json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture
def http(monkeypatch):
    """A fake aiohttp plus Home Assistant's shared client session."""
    aio = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total
    aio.ClientTimeout = ClientTimeout
    monkeypatch.setitem(sys.modules, "aiohttp", aio)
    holder = types.SimpleNamespace(session=_Session([]))
    ac = sys.modules["homeassistant.helpers.aiohttp_client"]
    monkeypatch.setattr(ac, "async_get_clientsession", lambda hass: holder.session,
                        raising=False)
    return holder


def _llm_world(monkeypatch, sh, provider, base=None, breaker=None, resolver_exc=None):
    sh.settings["llm_provider"] = provider
    seen = {}

    def _resolve(config, prov):
        seen["config"], seen["provider"] = dict(config), prov
        if resolver_exc:
            raise resolver_exc
        return base
    _fake(monkeypatch, "llm_provider", resolve_provider_endpoint=_resolve)

    def _status():
        if isinstance(breaker, BaseException):
            raise breaker
        return breaker
    _fake(monkeypatch, "connectivity", status=_status)
    return seen


# ── usage tracking ──────────────────────────────────────────────────────────

def test_constants_and_status_words(sh):
    m = sh.mod
    assert (m._OK, m._IDLE, m._WARN, m._DOWN, m._OFF) == ("ok", "idle", "warn", "down", "off")
    assert m._REAL_FAIL_TTL == 900.0 and m._RECENT_USE_TTL == 900.0


def test_record_usage_shapes_the_record(sh, monkeypatch):
    m = sh.mod
    monkeypatch.setattr(m.time, "time", lambda: 100.0)
    m.record_usage("stt", False)
    assert m._USAGE["stt"] == {"fail_ts": 100.0, "fail_detail": "a real request failed"}
    monkeypatch.setattr(m.time, "time", lambda: 150.0)
    m.record_usage("stt", True, "ignored")
    assert m._USAGE["stt"] == {"ok_ts": 150.0}
    monkeypatch.setattr(m.time, "time", lambda: 160.0)
    m.record_usage("stt", False, "boom")
    assert m._USAGE["stt"] == {"ok_ts": 150.0, "fail_ts": 160.0, "fail_detail": "boom"}
    assert m._recent_real_failure("stt") == "boom"


def test_record_usage_never_raises(sh, monkeypatch):
    monkeypatch.setattr(sh.mod, "_USAGE", None)
    assert sh.mod.record_usage("llm", True) is None


def test_failure_window_edges(sh, monkeypatch):
    m = sh.mod
    m._USAGE["llm"] = {"fail_ts": 1000.0}
    monkeypatch.setattr(m.time, "time", lambda: 1900.0)        # exactly the window
    assert m._recent_real_failure("llm") == "a real request failed"
    monkeypatch.setattr(m.time, "time", lambda: 1900.5)
    assert m._recent_real_failure("llm") is None
    m._USAGE["llm"] = {"fail_ts": 1000.0, "ok_ts": 1000.0, "fail_detail": "x"}
    monkeypatch.setattr(m.time, "time", lambda: 1001.0)
    assert m._recent_real_failure("llm") is None                 # same-moment success clears it
    m._USAGE["llm"] = {"fail_ts": 0, "fail_detail": "x"}
    assert m._recent_real_failure("llm") is None                 # a zero stamp counts as none


def test_recent_use_window_edges(sh, monkeypatch):
    m = sh.mod
    m._USAGE["tts"] = {"ok_ts": 1000.0}
    monkeypatch.setattr(m.time, "time", lambda: 1900.0)
    assert m._recently_used_ok("tts") is True
    monkeypatch.setattr(m.time, "time", lambda: 1900.1)
    assert m._recently_used_ok("tts") is False


# ── helpers ─────────────────────────────────────────────────────────────────

async def test_retry_async_sleeps_between_attempts_only(sh, no_sleep):
    calls = []

    async def _miss():
        calls.append(1)
        raise RuntimeError("cold")
    res = await sh.mod._retry_async(_miss)
    assert res == {"ok": False, "error": "cold"}
    assert len(calls) == 3 and no_sleep == [1.2, 1.2]


async def test_retry_async_non_dict_result_is_returned_last(sh, no_sleep):
    async def _none():
        return None
    assert await sh.mod._retry_async(_none, attempts=2, delay=0.5) is None
    assert no_sleep == [0.5]


def test_cfg_reads_settings_and_falls_back(load, monkeypatch):
    m = load("diagnostics.service_health")
    store = {"a": 1, "b": None}
    _fake(monkeypatch, "nova_config", get=lambda k, d=None: store.get(k, d))
    assert m._cfg("a") == 1
    assert m._cfg("b", "dflt") == "dflt"
    assert m._cfg("missing", 7) == 7

    def _boom(k, d=None):
        raise RuntimeError("no config")
    _fake(monkeypatch, "nova_config", get=_boom)
    assert m._cfg("a", "safe") == "safe"


@pytest.mark.parametrize("url,expected", [
    ("https://u:p@host/x@y", "https://host/x@y"),
    ("user@host", "user@host"),
    ("http://host", "http://host"),
])
def test_redact(sh, url, expected):
    assert sh.mod._redact(url) == expected


def test_label_prefers_a_distinct_friendly_name(sh):
    S = types.SimpleNamespace
    assert sh.mod._label(S(entity_id="tts.a", attributes={"friendly_name": " Engine "})) \
        == "Engine (tts.a)"
    assert sh.mod._label(S(entity_id="tts.a", attributes={"friendly_name": "tts.a"})) == "tts.a"
    assert sh.mod._label(S(entity_id="tts.a", attributes=None)) == "tts.a"
    assert sh.mod._label(S(entity_id="tts.a")) == "tts.a"         # no attributes at all


# ── LLM ─────────────────────────────────────────────────────────────────────

async def test_llm_no_provider_reads_as_cloud_ok(sh, monkeypatch):
    _llm_world(monkeypatch, sh, "", breaker=RuntimeError("no breaker"))
    out = await sh.mod._check_llm(None)
    assert out == {"name": "LLM", "key": "llm", "status": "ok",
                   "detail": "cloud provider — no recent failures"}


async def test_llm_falls_back_to_the_old_provider_key(sh, monkeypatch):
    _llm_world(monkeypatch, sh, "", breaker={"state": "OPEN"})
    sh.settings["provider"] = " groq "
    out = await sh.mod._check_llm(None)
    assert out["status"] == "warn" and out["breaker"] == "OPEN"
    assert out["detail"] == "groq provider — breaker OPEN (recent failures)"


async def test_llm_self_hosted_without_endpoint_is_off(sh, monkeypatch):
    _llm_world(monkeypatch, sh, "custom", base=None)
    out = await sh.mod._check_llm(None)
    assert out["status"] == "off" and out["detail"] == "no custom endpoint configured"


async def test_llm_resolver_value_error_counts_as_no_endpoint(sh, monkeypatch):
    _llm_world(monkeypatch, sh, "ollama", resolver_exc=ValueError("bad url"))
    out = await sh.mod._check_llm(None)
    assert out["detail"] == "no ollama endpoint configured"


async def test_llm_endpoint_lookup_hands_on_the_migration_flag(sh, monkeypatch):
    """Fixed (bugs.md #4): the migration flag is handed on, so the legacy
    llm_base_url is no longer a fallback once an installation has migrated."""
    seen = _llm_world(monkeypatch, sh, "custom", base="http://u:p@gpu:8000/v1",
                      breaker={"state": "CLOSED"})
    sh.settings.update(custom_base_url="http://gpu:8000/v1",
                       self_hosted_endpoints_migrated=True)
    out = await sh.mod._check_llm(None)
    assert seen["config"] == {"ollama_base_url": "", "custom_base_url": "http://gpu:8000/v1",
                              "llm_base_url": "", "self_hosted_endpoints_migrated": True}
    assert seen["provider"] == "custom"
    assert out == {"name": "LLM", "key": "llm", "status": "ok",
                   "detail": "custom provider — no recent failures",
                   "base": "http://gpu:8000/v1", "breaker": "CLOSED"}


async def test_llm_real_failure_wins_and_carries_breaker(sh, monkeypatch):
    _llm_world(monkeypatch, sh, "groq", breaker={"state": "OPEN"})
    sh.mod.record_usage("llm", False, "quota")
    out = await sh.mod._check_llm(None)
    assert out == {"name": "LLM", "key": "llm", "status": "down",
                   "detail": "a real agent call failed: quota", "breaker": "OPEN"}


async def test_llm_ollama_reachable(sh, monkeypatch, http, no_sleep):
    _llm_world(monkeypatch, sh, "ollama", base="http://a:b@gpu:11434/v1/",
               breaker={"state": "CLOSED"})
    http.session = _Session([_Resp(200, {"models": [{}, {}]})])
    out = await sh.mod._check_llm(None)
    assert out["status"] == "ok"
    assert out["detail"] == "reachable — 2 model(s) available"
    assert out["base"] == "http://gpu:11434/v1/"
    assert out["breaker"] == "CLOSED"
    url, timeout = http.session.calls[0]
    assert url == "http://a:b@gpu:11434/api/tags" and timeout.total == 8
    assert no_sleep == []


async def test_llm_ollama_retries_once_then_idle(sh, monkeypatch, http, no_sleep):
    _llm_world(monkeypatch, sh, "ollama", base="http://gpu:11434", breaker=None)
    http.session = _Session([_Resp(500), OSError("refused")])
    out = await sh.mod._check_llm(None)
    assert no_sleep == [1.0] and len(http.session.calls) == 2
    assert out["status"] == "idle" and out["detail"] == "unreachable: refused"
    assert "breaker" not in out


async def test_llm_ollama_second_ping_succeeds(sh, monkeypatch, http, no_sleep):
    _llm_world(monkeypatch, sh, "ollama", base="http://gpu:11434")
    http.session = _Session([_Resp(503), _Resp(200, {"models": None})])
    out = await sh.mod._check_llm(None)
    assert out["status"] == "ok" and out["detail"] == "reachable — 0 model(s) available"


async def test_llm_ollama_ping_miss_with_recent_use_is_ok(sh, monkeypatch, http, no_sleep):
    _llm_world(monkeypatch, sh, "ollama", base="http://gpu:11434")
    sh.mod.record_usage("llm", True)
    http.session = _Session([_Resp(404), _Resp(404)])
    out = await sh.mod._check_llm(None)
    assert out["status"] == "ok"
    assert out["detail"] == "working (recent agent call succeeded; ping missed)"


async def test_llm_provider_name_ignores_case(sh, monkeypatch, http, no_sleep):
    """Fixed (bugs.md #5): 'Ollama' is read as 'ollama' and pinged."""
    _llm_world(monkeypatch, sh, "Ollama", base="http://gpu:11434")
    http.session = _Session([_Resp(200, {"models": [{}]})])
    out = await sh.mod._check_llm(None)
    assert out["status"] == "ok" and out["detail"] == "reachable — 1 model(s) available"


async def test_ping_detail_never_shows_the_credentials(sh, http):
    """Fixed (bugs.md #2): a failed ping names the endpoint without its
    user:password@ part, whether the server answered or not."""
    http.session = _Session([_Resp(401)])
    ok, detail = await sh.mod._ping_ollama(None, "http://u:p@gpu:11434/")
    assert ok is False and detail == "HTTP 401 from http://gpu:11434"
    http.session = _Session([OSError("cannot reach http://u:p@gpu:11434/api/tags")])
    ok, detail = await sh.mod._ping_ollama(None, "http://u:p@gpu:11434")
    assert ok is False and detail == "unreachable: cannot reach http://gpu:11434/api/tags"


async def test_ping_unparseable_body_is_still_reachable(sh, http):
    http.session = _Session([_Resp(200, bad_json=True)])
    assert await sh.mod._ping_ollama(None, "http://gpu:11434") == (True, "reachable")


async def test_ping_without_aiohttp_is_unreachable(sh, monkeypatch):
    monkeypatch.setitem(sys.modules, "aiohttp", None)
    ok, detail = await sh.mod._ping_ollama(None, "http://gpu:11434")
    assert ok is False and detail.startswith("unreachable: ")


# ── embeddings ──────────────────────────────────────────────────────────────

def _emb(monkeypatch, enabled=True, result=None, exc=None):
    calls = []

    async def _probe(hass):
        calls.append(hass)
        if exc:
            raise exc
        return result

    def _is_enabled():
        if isinstance(enabled, BaseException):
            raise enabled
        return enabled
    _fake(monkeypatch, "embeddings", is_enabled=_is_enabled, probe=_probe)
    return calls


async def test_embeddings_disabled_is_off(sh, monkeypatch):
    _emb(monkeypatch, enabled=False)
    out = await sh.mod._check_embeddings(None)
    assert out == {"name": "Embeddings", "key": "embeddings", "status": "off",
                   "detail": "semantic search disabled (keyword search active)"}


async def test_embeddings_probe_ok_names_model_and_size(sh, monkeypatch, no_sleep):
    calls = _emb(monkeypatch, result={"ok": True, "model": "nomic", "dim": 768})
    out = await sh.mod._check_embeddings("H")
    assert out["status"] == "ok" and out["detail"] == "Ollama nomic — 768-dim vectors"
    assert calls == ["H"]


async def test_embeddings_probe_miss_retries_then_idle(sh, monkeypatch, no_sleep):
    calls = _emb(monkeypatch, exc=RuntimeError("cold"))
    out = await sh.mod._check_embeddings(None)
    assert len(calls) == 3 and no_sleep == [1.2, 1.2]
    assert out["status"] == "idle"
    assert out["detail"] == ("not exercised recently — the embed model may be unloaded "
                             "at idle; it loads on demand when you ingest or search documents")


async def test_embeddings_recent_use_covers_a_miss(sh, monkeypatch, no_sleep):
    _emb(monkeypatch, result={"ok": False})
    sh.mod.record_usage("embeddings", True)
    out = await sh.mod._check_embeddings(None)
    assert out["detail"] == "working (recent embedding succeeded; model idle for the health poke)"


async def test_embeddings_real_failure_skips_the_probe(sh, monkeypatch):
    calls = _emb(monkeypatch, result={"ok": True})
    sh.mod.record_usage("embeddings", False)
    out = await sh.mod._check_embeddings(None)
    assert out["detail"] == "a real embedding request failed: a real request failed"
    assert calls == []


async def test_embeddings_check_error_is_idle(sh, monkeypatch):
    _emb(monkeypatch, enabled=RuntimeError("broken"))
    out = await sh.mod._check_embeddings(None)
    assert out["status"] == "idle" and out["detail"] == "health check could not verify: broken"


# ── TTS and STT ─────────────────────────────────────────────────────────────

def test_tts_and_stt_read_their_engine_settings(sh, monkeypatch):
    seen = []
    monkeypatch.setattr(sh.mod, "_check_speech_entity",
                        lambda hass, domain, name, configured: seen.append(
                            (domain, name, configured)) or {})
    sh.settings["stt_engine"] = "stt.local"
    sh.settings["tts_engine"] = ""
    sh.mod._check_tts(None)
    sh.mod._check_stt(None)
    assert seen == [("tts", "TTS", "auto"), ("stt", "STT", "stt.local")]


def test_speech_none_registered_but_recently_used(sh, fake_hass):
    sh.mod.record_usage("tts", True)
    out = sh.mod._check_speech_entity(fake_hass, "tts", "TTS", "auto")
    assert out == {"name": "TTS", "key": "tts", "status": "ok",
                   "detail": "tts used successfully recently"}


def test_speech_none_registered(sh):
    class _Broken:
        class states:
            @staticmethod
            def async_all(domain):
                raise RuntimeError("no states")
    out = sh.mod._check_speech_entity(_Broken(), "stt", "STT", "auto")
    assert out["status"] == "idle"
    assert out["detail"] == "no stt engine registered (install the add-on to enable)"


def test_speech_auto_counts_available_engines(sh, fake_hass):
    fake_hass.states.set("stt.a", "unknown")
    fake_hass.states.set("stt.b", "idle")
    fake_hass.states.set("stt.c", "idle")
    out = sh.mod._check_speech_entity(fake_hass, "stt", "STT", "")
    assert out["detail"] == "2 engine(s) available (auto)" and out["entity"] == "stt.b"


def test_speech_auto_idle_details(sh, fake_hass):
    fake_hass.states.set("tts.a", "unavailable")
    out = sh.mod._check_speech_entity(fake_hass, "tts", "TTS", "auto")
    assert out["detail"] == ("1 engine(s) present, idle — they come available on "
                             "demand when tts is used")
    assert "entity" not in out
    sh.mod.record_usage("tts", True)
    out = sh.mod._check_speech_entity(fake_hass, "tts", "TTS", "auto")
    assert out["status"] == "ok"
    assert out["detail"] == "1 engine(s) present, idle (recent tts succeeded)"


def test_speech_specific_matches_by_substring(sh, fake_hass):
    fake_hass.states.set("tts.cloud", "idle")
    fake_hass.states.set("tts.piper_local", "unavailable", friendly_name="Piper")
    out = sh.mod._check_speech_entity(fake_hass, "tts", "TTS", "piper")
    assert out["status"] == "idle" and out["entity"] == "tts.piper_local"
    assert out["detail"] == "Piper (tts.piper_local) idle — comes available on demand"
    sh.mod.record_usage("tts", True)
    out = sh.mod._check_speech_entity(fake_hass, "tts", "TTS", "piper")
    assert out["status"] == "ok"
    assert out["detail"] == "Piper (tts.piper_local) idle (recent tts succeeded)"


def test_speech_specific_missing_counts_the_others(sh, fake_hass):
    fake_hass.states.set("stt.a", "idle")
    fake_hass.states.set("stt.b", "idle")
    out = sh.mod._check_speech_entity(fake_hass, "stt", "STT", "stt.x")
    assert out["detail"] == "configured 'stt.x' not found; 2 other(s) present"


def test_speech_real_failure_detail(sh, fake_hass):
    sh.mod.record_usage("tts", False, "all speakers failed")
    out = sh.mod._check_speech_entity(fake_hass, "tts", "TTS", "auto")
    assert out == {"name": "TTS", "key": "tts", "status": "down",
                   "detail": "a real tts request failed: all speakers failed"}


# ── cameras ─────────────────────────────────────────────────────────────────

def test_cameras_count_none_and_blank_states_as_down(sh, fake_hass):
    fake_hass.states.set("camera.a", "None")
    fake_hass.states.set("camera.b", "")
    fake_hass.states.set("camera.c", "UNKNOWN")
    fake_hass.states.set("camera.d", "recording")
    out = sh.mod._check_cameras(fake_hass)
    assert out == {"name": "Cameras", "key": "cameras", "status": "warn",
                   "detail": "1/4 available, 3 unavailable"}


def test_cameras_all_ok_detail(sh, fake_hass):
    fake_hass.states.set("camera.a", "idle")
    assert sh.mod._check_cameras(fake_hass)["detail"] == "1 camera(s) available"


# ── routines ────────────────────────────────────────────────────────────────

def test_routines_details(sh):
    out = sh.mod._check_routines(None)
    assert out == {"name": "Routines", "key": "routines", "status": "ok",
                   "detail": "identity confidence 0.45 — attribution active"}
    sh.settings["identity_min_confidence"] = "0.85"
    out = sh.mod._check_routines(None)
    assert out["status"] == "warn"
    assert out["detail"] == ("identity confidence 0.85 is very high — per-person "
                             "routines rarely attribute; lower to ~0.5–0.6")


def test_routines_bad_setting_is_idle(sh):
    sh.settings["identity_min_confidence"] = "x" * 200
    out = sh.mod._check_routines(None)
    assert out["status"] == "idle" and len(out["detail"]) == 80


# ── conversation store ──────────────────────────────────────────────────────

def _db(monkeypatch, exists=True, health=None):
    def _path():
        if isinstance(exists, BaseException):
            raise exists
        return types.SimpleNamespace(exists=lambda: exists)

    def _health():
        if isinstance(health, BaseException):
            raise health
        return health
    _fake(monkeypatch, "database", _db_path=_path, health=_health)


def test_database_missing_path_or_path_error_is_off(sh, monkeypatch):
    _db(monkeypatch, exists=False)
    assert sh.mod._check_database(None)["detail"] == "not yet created"
    _db(monkeypatch, exists=OSError("no home"))
    assert sh.mod._check_database(None)["detail"] == "not yet created"


def test_database_probe_outcomes(sh, monkeypatch):
    _db(monkeypatch, health=RuntimeError("locked"))
    assert sh.mod._check_database(None) == {
        "name": "Conversation store", "key": "database", "status": "down",
        "detail": "health probe error: locked"}
    _db(monkeypatch, health={"ok": False})
    assert sh.mod._check_database(None)["detail"] == "schema/migration error: unknown"
    _db(monkeypatch, health={"ok": True})
    assert sh.mod._check_database(None)["detail"] == "conversation store OK"


def test_database_module_unimportable_is_off(sh, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.database", None)
    monkeypatch.delattr(sys.modules["jc"], "database", raising=False)
    out = sh.mod._check_database(None)
    assert out["status"] == "off" and out["detail"] == "database module unavailable"


# ── Voice ID and scheduler (both read the entry's runtime) ──────────────────

class _NoRuntime(RuntimeError):
    """Stands in for runtime.NovaRuntimeUnavailable."""


def _runtime(monkeypatch, runtime=None, exc=None):
    def _dr(hass):
        if exc:
            raise exc
        return runtime
    _fake(monkeypatch, "runtime", domain_runtime=_dr, NovaRuntimeUnavailable=_NoRuntime)


def _voice(monkeypatch, status):
    _runtime(monkeypatch, types.SimpleNamespace(
        voice_id=types.SimpleNamespace(status=lambda: status)))


def test_voice_id_off_without_runtime(sh, monkeypatch):
    _runtime(monkeypatch, None)
    assert sh.mod._check_voice_id(None) == {
        "name": "Voice ID", "key": "voice_id", "status": "off", "detail": "no app address set"}


def test_voice_id_details(sh, monkeypatch):
    _voice(monkeypatch, {"state": "connected", "version": "1.2"})
    assert sh.mod._check_voice_id(None)["detail"] == "app connected (v1.2)"
    _voice(monkeypatch, {"state": "connected"})
    assert sh.mod._check_voice_id(None)["detail"] == "app connected"
    _voice(monkeypatch, {"state": "unreachable"})
    assert sh.mod._check_voice_id(None)["detail"] == \
        "app not reachable at the last check; the fallback is used"
    _voice(monkeypatch, {"state": "checking"})
    assert sh.mod._check_voice_id(None)["detail"] == "checking the app"


def test_voice_id_unreachable_outranks_recent_use(sh, monkeypatch):
    """Fixed (bugs.md #6): the app's own 'unreachable' state wins over a
    recent successful request, so the row no longer says 'app connected'."""
    _voice(monkeypatch, {"state": "unreachable"})
    sh.mod.record_usage("voice_id", True)
    out = sh.mod._check_voice_id(None)
    assert out["status"] == "idle"
    assert out["detail"] == "app not reachable at the last check; the fallback is used"
    _voice(monkeypatch, {"state": "checking"})
    assert sh.mod._check_voice_id(None)["detail"] == "app connected"


def test_voice_id_real_failure_detail(sh, monkeypatch):
    _voice(monkeypatch, {"state": "connected"})
    sh.mod.record_usage("voice_id", False, "stream broke")
    assert sh.mod._check_voice_id(None)["detail"] == "a real request failed: stream broke"


class _Sched:
    def __init__(self, tasks=None, exc=None):
        self.tasks, self.exc = tasks, exc

    def status(self):
        if self.exc:
            raise self.exc
        return self.tasks


def test_scheduler_outcomes(sh, monkeypatch):
    def run(sched):
        _runtime(monkeypatch, types.SimpleNamespace(scheduler=sched))
        return sh.mod._check_scheduler(None)
    assert run(None)["detail"] == "not running"
    assert run(_Sched(exc=RuntimeError("lock"))) == {
        "name": "Scheduler", "key": "scheduler", "status": "warn", "detail": "status error: lock"}
    out = run(_Sched([]))
    assert out["status"] == "off" and out["detail"] == "no periodic tasks"
    out = run(_Sched([{"name": "a", "consecutive_errors": 3}, {"name": "b"},
                      {"name": "c", "consecutive_errors": 9}]))
    assert out["detail"] == "2 task(s) failing: a, c"
    assert run(_Sched([{"name": "a", "consecutive_errors": 2}]))["detail"] == \
        "1 periodic task(s) scheduled"


async def test_runtime_lookup_error_reads_idle(sh, monkeypatch):
    """Fixed (bugs.md #3): a broken runtime lookup in Voice ID or the
    scheduler reads IDLE, as a broken check does everywhere else. A loaded
    entry without a runtime still raises, so the roll-up shows it DOWN."""
    _runtime(monkeypatch, exc=_NoRuntime("lost"))
    with pytest.raises(_NoRuntime):
        sh.mod._check_voice_id(None)
    with pytest.raises(_NoRuntime):
        sh.mod._check_scheduler(None)
    _runtime(monkeypatch, exc=RuntimeError("entry has no runtime"))
    _quiet_all(sh, monkeypatch, keep=("_check_voice_id", "_check_scheduler"))
    res = await sh.mod.run_service_health(None)
    rows = {s["key"]: s for s in res["services"]}
    assert rows["voice_id"] == {"name": "Voice ID", "key": "voice_id", "status": "idle",
                                "detail": "health check could not verify: entry has no runtime"}
    assert rows["scheduler"] == {"name": "Scheduler", "key": "scheduler", "status": "idle",
                                 "detail": "health check could not verify: entry has no runtime"}
    assert res["overall"] != "down"


# ── host health ─────────────────────────────────────────────────────────────

def _host(monkeypatch, snap):
    seen = {}

    def _snapshot(hass, config):
        seen["config"] = config
        if isinstance(snap, BaseException):
            raise snap
        return snap
    _fake(monkeypatch, "host_health", snapshot=_snapshot)
    _fake(monkeypatch, "nova_config", get_all=lambda: {"k": 1})
    return seen


@pytest.mark.parametrize("snap,status,detail", [
    ({"enabled": False}, "off", "not enabled (Settings → Host Health)"),
    ({"enabled": True, "overall": "problem",
      "persistent_problems": [{"label": "Disk"}, {"label": "Memory"}]},
     "warn", "persistent problem: Disk, Memory"),
    ({"enabled": True, "overall": "watch"}, "ok", "elevated reading(s), not yet persistent"),
    ({"enabled": True, "overall": "partial", "available": [1, 2], "missing_or_stale": [3]},
     "ok", "2 reading(s) active, 1 missing/stale"),
    ({"enabled": True, "overall": "ok", "available": [1]}, "ok", "1 reading(s) healthy"),
    ({"enabled": True, "overall": None}, "idle", "no readings yet"),
])
def test_host_health_rows(sh, monkeypatch, snap, status, detail):
    seen = _host(monkeypatch, snap)
    out = sh.mod._check_host_health("H")
    assert (out["status"], out["detail"]) == (status, detail)
    assert out["snapshot"] is snap and seen["config"] == {"k": 1}


def test_host_health_error_detail_is_trimmed(sh, monkeypatch):
    _host(monkeypatch, RuntimeError("z" * 300))
    out = sh.mod._check_host_health(None)
    assert out["status"] == "idle" and "snapshot" not in out
    assert out["detail"] == "host health check could not run: " + "z" * 120


# ── roll-up ─────────────────────────────────────────────────────────────────

_ORDER = [("LLM", "llm", "_check_llm"), ("Embeddings", "embeddings", "_check_embeddings"),
          ("TTS", "tts", "_check_tts"), ("STT", "stt", "_check_stt"),
          ("Voice ID", "voice_id", "_check_voice_id"), ("Cameras", "cameras", "_check_cameras"),
          ("Routines", "routines", "_check_routines"),
          ("Conversation store", "database", "_check_database"),
          ("Scheduler", "scheduler", "_check_scheduler"),
          ("Host health", "host_health", "_check_host_health")]
_ASYNC = {"_check_llm", "_check_embeddings"}


def _quiet_all(sh, monkeypatch, statuses=None, keep=()):
    """Replace every check with one that returns a fixed status."""
    statuses = statuses or {}
    for label, key, fn in _ORDER:
        if fn in keep:
            continue
        row = {"name": label, "key": key, "status": statuses.get(key, "off"), "detail": ""}
        if fn in _ASYNC:
            async def _a(hass, row=row):
                return row
            monkeypatch.setattr(sh.mod, fn, _a)
        else:
            monkeypatch.setattr(sh.mod, fn, lambda hass, row=row: row)


async def test_rollup_order_and_all_off(sh, monkeypatch):
    _quiet_all(sh, monkeypatch)
    res = await sh.mod.run_service_health(None)
    assert [s["key"] for s in res["services"]] == [k for _, k, _ in _ORDER]
    assert res["overall"] == "off" and res["summary"] == "no core services active"
    assert set(res) == {"overall", "services", "summary"}


async def test_rollup_failure_names_the_check(sh, monkeypatch):
    _quiet_all(sh, monkeypatch, {"llm": "ok", "tts": "idle"})

    def _boom(hass):
        raise KeyError("x")
    monkeypatch.setattr(sh.mod, "_check_cameras", _boom)
    res = await sh.mod.run_service_health(None)
    assert res["services"][5] == {"name": "Cameras", "key": "cameras", "status": "down",
                                  "detail": "'x'"}
    assert res["overall"] == "down"
    assert res["summary"] == "1 service(s) failing — 1/3 healthy"


async def test_rollup_idle_summary(sh, monkeypatch):
    _quiet_all(sh, monkeypatch, {"llm": "ok", "stt": "idle", "tts": "idle"})
    res = await sh.mod.run_service_health(None)
    assert res["overall"] == "ok"
    assert res["summary"] == "1/3 healthy, 2 idle (not recently used)"


async def test_rollup_all_healthy_summary(sh, monkeypatch):
    _quiet_all(sh, monkeypatch, {"llm": "ok", "routines": "ok"})
    res = await sh.mod.run_service_health(None)
    assert res["summary"] == "all 2 core services healthy"


async def test_rollup_warn_rows_are_named_in_the_summary(sh, monkeypatch):
    """Fixed (bugs.md #1): with a WARN row and nothing down, the summary no
    longer says every service is healthy."""
    _quiet_all(sh, monkeypatch, {"llm": "ok", "cameras": "warn", "tts": "idle"})
    res = await sh.mod.run_service_health(None)
    assert res["overall"] == "warn"
    assert res["summary"] == "1 service(s) need a look — 1/3 healthy"


async def test_rollup_runs_the_real_checks_without_raising(sh, monkeypatch, fake_hass):
    """No fakes for the checks themselves: on a bare hass every row is
    produced and nothing escapes."""
    _llm_world(monkeypatch, sh, "groq", breaker={"state": "CLOSED"})
    _emb(monkeypatch, enabled=False)
    _runtime(monkeypatch, None)
    _db(monkeypatch, exists=False)
    _host(monkeypatch, {"enabled": False})
    res = await sh.mod.run_service_health(fake_hass)
    rows = {s["key"]: s["status"] for s in res["services"]}
    assert rows == {"llm": "ok", "embeddings": "off", "tts": "idle", "stt": "idle",
                    "voice_id": "off", "cameras": "off", "routines": "ok",
                    "database": "off", "scheduler": "off", "host_health": "off"}
    assert res["summary"] == "2/4 healthy, 2 idle (not recently used)"
