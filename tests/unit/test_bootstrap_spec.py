"""The in-process bootstrap, checked against its spec (7e).

On HA OS / Supervised the bootstrap makes sure the Piper, Whisper and
openWakeWord add-ons are installed and running, reloads Wyoming and builds or
repairs the Nova Assist pipeline, once per version. On every start, wherever
Nova runs, it points any Nova pipeline at Nova's own conversation entity and
then hands over to the welcome notice. These pin what test_bootstrap.py
leaves open: the Supervisor REST calls, the add-on install and start polling,
agent and engine discovery, every pipeline failure path, the marker file, the
order and gating of the background runner, and the handle's shutdown.
Everything outward is faked: the HTTP session, sleeps, the assist_pipeline
component, the registry, config entries and the notices.
"""
import contextlib
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeHass

# Same aiohttp stub as test_bootstrap.py; whichever file runs first installs it.
if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


@pytest.fixture
def bs(load, tmp_path, monkeypatch):
    mod = sys.modules.get("jc.bootstrap")
    if mod is not None and not hasattr(mod, "schedule_bootstrap"):
        del sys.modules["jc.bootstrap"]
    m = load("bootstrap")
    monkeypatch.setattr(m, "MARKER_PATH", Path(tmp_path / "marker" / ".bootstrap_done"))
    monkeypatch.setattr(m, "PIPER_DIR", Path(tmp_path / "piper"))
    return m


@pytest.fixture
def sleeps(bs, monkeypatch):
    """Sleeps are recorded, never waited."""
    taken = []

    async def _sleep(seconds):
        taken.append(seconds)
    monkeypatch.setattr(bs, "asyncio", SimpleNamespace(sleep=_sleep))
    return taken


# ── Supervisor session fake ─────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Answers each (method, path) from a script: a list is consumed in
    order (its last entry repeats), an Exception raises on request."""

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.calls = []

    def request(self, method, url, headers=None, timeout=None):
        path = url[len("http://supervisor"):]
        self.calls.append((method, url, headers, timeout))
        answer = self.script.get((method, path), (404, None))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return _Resp(*answer)

    def paths(self):
        return [(m, u[len("http://supervisor"):]) for m, u, _, _ in self.calls]


@pytest.fixture
def session(bs, monkeypatch, sleeps):
    s = _Session()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setattr(bs, "async_get_clientsession", lambda hass: s)
    monkeypatch.setattr(bs, "aiohttp",
                        SimpleNamespace(ClientTimeout=lambda total: ("timeout", total)))
    return s


def _info(version="1.0", state="started"):
    return (200, {"data": {"version": version, "state": state}})


# ── constants and names ─────────────────────────────────────────────────────

def test_constants_and_addon_order(bs):
    assert bs.SUPERVISOR == "http://supervisor"
    assert bs.LEGACY_VOICE_PREFIX == "en_GB-nova-"
    assert list(bs.REQUIRED_ADDONS.items()) == [
        ("core_piper", "Piper TTS"), ("core_whisper", "Whisper STT"),
        ("core_openwakeword", "openWakeWord")]


def test_piper_dir_and_marker_override_defaults(load):
    import importlib.util
    spec = importlib.util.find_spec("jc.bootstrap")
    assert spec is not None
    src = Path(spec.origin).read_text(encoding="utf-8")
    assert 'PIPER_DIR = Path("/share/piper")' in src
    assert "MARKER_PATH: Optional[Path] = None" in src


def test_marker_defaults_under_nova_folder(bs, load, tmp_path, monkeypatch):
    paths = load("paths")
    monkeypatch.setattr(paths, "_root", str(tmp_path / "cfg"))
    monkeypatch.setattr(bs, "MARKER_PATH", None)
    bs._write_marker("1.2.3", {"addons_ok": True})
    stored = tmp_path / "cfg" / "nova" / ".bootstrap_done"
    assert json.loads(stored.read_text()) == {"version": "1.2.3", "addons_ok": True}
    assert bs._read_marker()["version"] == "1.2.3"


def test_storage_contract_names_the_marker():
    import contract_extract
    assert contract_extract.storage_contract()["bootstrap"] == {
        "paths": ["nova/.bootstrap_done"], "tables": []}


def test_empty_token_is_not_supervised(bs, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    assert bs.supervisor_token() == "" and bs.is_supervised() is False


# ── Supervisor REST ─────────────────────────────────────────────────────────

async def test_info_sends_bearer_token_url_and_timeout(bs, session):
    session.script[("GET", "/addons/core_piper/info")] = _info()
    assert await bs._ensure_addon(None, "core_piper", "Piper TTS") is True
    assert len(session.calls) == 1
    method, url, headers, timeout = session.calls[0]
    assert (method, url) == ("GET", "http://supervisor/addons/core_piper/info")
    assert headers == {"Authorization": "Bearer tok"}
    assert timeout == ("timeout", 60)


@pytest.mark.parametrize("answer", [
    (200, {"result": "ok"}),                 # no "data"
    (404, {"data": {"version": "1"}}),       # not a 200
    (200, ValueError("not json")),           # body is not JSON
    (200, {"data": {"state": "started"}}),   # no version yet
])
async def test_unusable_info_reads_as_not_installed(bs, session, answer):
    session.script[("GET", "/addons/a/info")] = answer
    assert await bs._ensure_addon(None, "a", "A") is False
    assert session.paths() == [("GET", "/addons/a/info"),
                               ("POST", "/store/addons/a/install"),
                               ("POST", "/addons/a/install")]


async def test_request_failure_logs_warning_and_reads_as_missing(bs, session, caplog):
    session.script[("GET", "/addons/x/info")] = OSError("refused")
    with caplog.at_level(logging.WARNING, logger=bs.__name__):
        assert await bs._ensure_addon(None, "x", "X") is False
    assert "Nova bootstrap: supervisor GET /addons/x/info failed: refused" in caplog.text
    assert ("POST", "/store/addons/x/install") in session.paths()


async def test_install_through_the_store_stops_there(bs, session, sleeps):
    session.script[("GET", "/addons/s1/info")] = [(404, None), _info()]
    session.script[("POST", "/store/addons/s1/install")] = (202, None)
    assert await bs._ensure_addon(None, "s1", "S1") is True
    assert session.paths() == [("GET", "/addons/s1/info"),
                               ("POST", "/store/addons/s1/install"),
                               ("GET", "/addons/s1/info")]
    assert session.calls[1][3] == ("timeout", 600)


async def test_install_falls_back_when_the_store_refuses(bs, session, sleeps):
    session.script[("GET", "/addons/s2/info")] = [(404, None), _info()]
    session.script[("POST", "/store/addons/s2/install")] = (500, None)
    session.script[("POST", "/addons/s2/install")] = (200, None)
    assert await bs._ensure_addon(None, "s2", "S2") is True
    assert session.paths()[1:3] == [("POST", "/store/addons/s2/install"),
                                    ("POST", "/addons/s2/install")]
    assert session.calls[2][3] == ("timeout", 600)


# ── ensure add-on ───────────────────────────────────────────────────────────

async def test_running_addon_needs_nothing(bs, session, sleeps):
    session.script[("GET", "/addons/p/info")] = _info(state="started")
    assert await bs._ensure_addon(None, "p", "Piper") is True
    assert session.paths() == [("GET", "/addons/p/info")]
    assert sleeps == []


async def test_stopped_addon_is_started_and_waited_for(bs, session, sleeps, caplog):
    session.script[("GET", "/addons/p/info")] = [
        _info(state="stopped"), _info(state="stopped"), _info(state="started")]
    session.script[("POST", "/addons/p/start")] = (200, None)
    with caplog.at_level(logging.INFO, logger=bs.__name__):
        assert await bs._ensure_addon(None, "p", "Piper") is True
    start = session.paths().index(("POST", "/addons/p/start"))
    assert session.calls[start][3] == ("timeout", 60)
    assert sleeps == [2.0]
    assert "Nova bootstrap: starting Piper" in caplog.text


async def test_start_wait_gives_up_after_thirty_polls(bs, session, sleeps):
    session.script[("GET", "/addons/p/info")] = _info(state="stopped")
    session.script[("POST", "/addons/p/start")] = (200, None)
    assert await bs._ensure_addon(None, "p", "Piper") is False
    assert sleeps == [2.0] * 30


async def test_failed_start_is_false(bs, session, sleeps):
    session.script[("GET", "/addons/p/info")] = _info(state="stopped")
    assert await bs._ensure_addon(None, "p", "Piper") is False
    assert sleeps == []


async def test_missing_addon_installs_polls_then_starts(bs, session, sleeps, caplog):
    session.script[("GET", "/addons/w/info")] = [
        (200, {"data": {"version": None, "state": "unknown"}}),
        (200, {"data": {"version": None}}),
        _info(state="stopped"),
        _info(state="started")]
    session.script[("POST", "/store/addons/w/install")] = (200, None)
    session.script[("POST", "/addons/w/start")] = (200, None)
    with caplog.at_level(logging.INFO, logger=bs.__name__):
        assert await bs._ensure_addon(None, "w", "Whisper") is True
    assert sleeps == [5, 5]
    assert "Nova bootstrap: installing Whisper" in caplog.text


async def test_installed_addon_already_running_skips_start(bs, session, sleeps):
    session.script[("GET", "/addons/w/info")] = [(404, None), _info(state="started")]
    session.script[("POST", "/store/addons/w/install")] = (200, None)
    assert await bs._ensure_addon(None, "w", "Whisper") is True
    assert ("POST", "/addons/w/start") not in session.paths()


async def test_failed_install_is_false(bs, session, sleeps):
    assert await bs._ensure_addon(None, "w", "Whisper") is False
    assert sleeps == []


async def test_install_times_out_after_five_minutes(bs, session, sleeps, caplog):
    session.script[("POST", "/store/addons/w/install")] = (200, None)
    with caplog.at_level(logging.WARNING, logger=bs.__name__):
        assert await bs._ensure_addon(None, "w", "Whisper") is False
    assert sleeps == [5] * 60
    assert "Nova bootstrap: Whisper install timed out" in caplog.text


# ── legacy voice ────────────────────────────────────────────────────────────

def test_legacy_voice_is_a_file_under_piper_dir(bs):
    assert bs._legacy_voice_present("en_GB-nova-high") is False
    bs.PIPER_DIR.mkdir(parents=True)
    (bs.PIPER_DIR / "en_GB-nova-high.onnx").write_bytes(b"")
    assert bs._legacy_voice_present("en_GB-nova-high") is True


# ── Wyoming reload ──────────────────────────────────────────────────────────

class _Entries:
    def __init__(self, ids, failing=()):
        self.ids, self.failing, self.reloaded = list(ids), set(failing), []

    def async_entries(self, domain):
        assert domain == "wyoming"
        return [SimpleNamespace(entry_id=i) for i in self.ids]

    async def async_reload(self, entry_id):
        if entry_id in self.failing:
            raise RuntimeError("gone")
        self.reloaded.append(entry_id)


async def test_failed_reload_is_skipped_then_raised(bs, caplog):
    entries = _Entries(["w1", "w2", "w3"], failing={"w2"})
    with caplog.at_level(logging.INFO, logger=bs.__name__), \
            pytest.raises(RuntimeError, match="1 Wyoming reload"):
        await bs._reload_wyoming(SimpleNamespace(config_entries=entries))
    assert entries.reloaded == ["w1", "w3"]
    assert "reloaded 2 Wyoming entries" in caplog.text


async def test_one_reload_reads_entry(bs, caplog):
    with caplog.at_level(logging.INFO, logger=bs.__name__):
        await bs._reload_wyoming(SimpleNamespace(config_entries=_Entries(["w1"])))
    assert "reloaded 1 Wyoming entry" in caplog.text
    assert "entries" not in caplog.text


async def test_no_wyoming_entries_logs_nothing(bs, caplog):
    with caplog.at_level(logging.INFO, logger=bs.__name__):
        assert await bs._reload_wyoming(SimpleNamespace(config_entries=_Entries([]))) == 0
    assert caplog.text == ""


# ── agent and engine discovery ──────────────────────────────────────────────

@pytest.fixture
def registry(monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]
    reg = SimpleNamespace(entities={})
    monkeypatch.setattr(er, "async_get", lambda hass: reg)
    return reg


def _reg_entry(entity_id, platform):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0],
                           platform=platform)


def test_agent_comes_from_the_registry_first(bs, registry, fake_hass):
    registry.entities = {
        "a": _reg_entry("conversation.other", "openai"),
        "b": _reg_entry("sensor.nova_x", "nova"),
        "c": _reg_entry("conversation.assistant", "nova")}
    fake_hass.states.set("conversation.nova", "idle")
    assert bs._find_nova_agent(fake_hass) == "conversation.assistant"


def test_agent_falls_back_to_a_state_naming_nova(bs, registry, fake_hass):
    fake_hass.states.set("conversation.home", "idle")
    fake_hass.states.set("conversation.My_NOVA", "idle")
    assert bs._find_nova_agent(fake_hass) == "conversation.My_NOVA"


def test_registry_error_still_falls_back(bs, fake_hass, monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]

    def _boom(hass):
        raise RuntimeError("no registry")
    monkeypatch.setattr(er, "async_get", _boom)
    fake_hass.states.set("conversation.nova", "idle")
    assert bs._find_nova_agent(fake_hass) == "conversation.nova"


def test_no_agent_is_none(bs, registry, fake_hass):
    fake_hass.states.set("conversation.home", "idle")
    assert bs._find_nova_agent(fake_hass) is None


def test_engine_hint_is_case_insensitive_and_none_when_empty(bs, fake_hass):
    fake_hass.states.set("stt.cloud", "idle")
    fake_hass.states.set("stt.Faster_Whisper", "idle")
    assert bs._find_engine(fake_hass, "stt", "whisper") == "stt.Faster_Whisper"
    assert bs._find_engine(fake_hass, "tts", "piper") is None


async def test_agent_wait_polls_then_gives_up(bs, sleeps, monkeypatch):
    answers = [None, None, "conversation.nova"]
    monkeypatch.setattr(bs, "_find_nova_agent", lambda hass: answers.pop(0))
    assert await bs._wait_for_agent(None, tries=5, delay=1.5) == "conversation.nova"
    assert sleeps == [1.5, 1.5]

    sleeps.clear()
    monkeypatch.setattr(bs, "_find_nova_agent", lambda hass: None)
    assert await bs._wait_for_agent(None) is None
    assert sleeps == [3.0] * 40


# ── assist_pipeline fake ────────────────────────────────────────────────────

class _Pipe:
    def __init__(self, name="Nova", conversation_engine=None, tts_voice=None):
        self.name = name
        self.conversation_engine = conversation_engine
        self.tts_voice = tts_voice


class _AssistPipeline:
    def __init__(self, pipelines=(), created=None, create_error=None,
                 list_error=None, update_error=None):
        self.pipelines = list(pipelines)
        self.created = created
        self.create_error = create_error
        self.list_error = list_error
        self.update_error = update_error
        self.create_calls = []
        self.updates = []

    def async_get_pipelines(self, hass):
        if self.list_error:
            raise self.list_error
        return iter(self.pipelines)

    async def async_create_default_pipeline(self, hass, stt_engine_id, tts_engine_id,
                                            pipeline_name):
        self.create_calls.append((stt_engine_id, tts_engine_id, pipeline_name))
        if self.create_error:
            raise self.create_error
        return self.created

    async def async_update_pipeline(self, hass, pipeline, **kwargs):
        if self.update_error is not None and self.update_error(pipeline):
            raise RuntimeError("update refused")
        self.updates.append((pipeline.name, kwargs))
        for key, value in kwargs.items():
            setattr(pipeline, key, value)


@contextlib.contextmanager
def _component(fake):
    components = sys.modules["homeassistant.components"]
    sys.modules["homeassistant.components.assist_pipeline"] = fake
    components.assist_pipeline = fake
    try:
        yield fake
    finally:
        del sys.modules["homeassistant.components.assist_pipeline"]
        del components.assist_pipeline


@pytest.fixture
def engines(fake_hass, registry):
    for eid in ("conversation.nova", "stt.faster_whisper", "tts.piper"):
        fake_hass.states.set(eid, "idle")
    return fake_hass


# ── _create_pipeline ────────────────────────────────────────────────────────

async def test_without_the_component_it_logs_the_manual_steps(bs, fake_hass, caplog):
    with caplog.at_level(logging.INFO, logger=bs.__name__):
        assert await bs._create_pipeline(fake_hass) is False
    assert ("Nova bootstrap: set the pipeline up manually under Settings → Voice "
            "Assistants — Conversation: Nova, STT: faster-whisper, TTS: piper, "
            "Wake word: openWakeWord.") in caplog.text


async def test_missing_engine_cannot_build(bs, fake_hass, registry, caplog):
    fake_hass.states.set("conversation.nova", "idle")
    fake_hass.states.set("stt.faster_whisper", "idle")
    fake = _AssistPipeline(created=_Pipe())
    with _component(fake), caplog.at_level(logging.WARNING, logger=bs.__name__):
        assert await bs._create_pipeline(fake_hass) is False
    assert fake.create_calls == []
    assert "can't build pipeline yet" in caplog.text


async def test_new_pipeline_uses_found_engines_and_name(bs, engines):
    created = _Pipe()
    fake = _AssistPipeline(created=created)
    with _component(fake):
        assert await bs._create_pipeline(engines) is True
    assert fake.create_calls == [("stt.faster_whisper", "tts.piper", "Nova")]
    assert created.conversation_engine == "conversation.nova"


async def test_create_returning_nothing_is_false(bs, engines, caplog):
    with _component(_AssistPipeline(created=None)), \
            caplog.at_level(logging.WARNING, logger=bs.__name__):
        assert await bs._create_pipeline(engines) is False
    assert "default pipeline creation returned nothing" in caplog.text


async def test_create_raising_is_false(bs, engines, caplog):
    fake = _AssistPipeline(create_error=RuntimeError("bad"))
    with _component(fake), caplog.at_level(logging.WARNING, logger=bs.__name__):
        assert await bs._create_pipeline(engines) is False
    assert "pipeline creation failed (bad)" in caplog.text


async def test_agent_set_failure_after_create_still_counts(bs, engines, caplog):
    fake = _AssistPipeline(created=_Pipe(), update_error=lambda p: True)
    with _component(fake), caplog.at_level(logging.WARNING, logger=bs.__name__):
        assert await bs._create_pipeline(engines) is True
    assert "couldn't set the agent" in caplog.text


async def test_listing_error_means_create_new(bs, engines):
    fake = _AssistPipeline(created=_Pipe(), list_error=RuntimeError("x"))
    with _component(fake):
        assert await bs._create_pipeline(engines) is True
    assert len(fake.create_calls) == 1


async def test_existing_match_is_by_name_any_case(bs, engines):
    other = _Pipe(name="Home Assistant", conversation_engine="conversation.home")
    mine = _Pipe(name="My NOVA", conversation_engine="conversation.home")
    fake = _AssistPipeline(pipelines=[other, mine])
    with _component(fake):
        assert await bs._create_pipeline(engines) is True
    assert fake.create_calls == []
    assert fake.updates == [("My NOVA", {"conversation_engine": "conversation.nova"})]


async def test_existing_repair_of_agent_and_voice_together(bs, engines, caplog):
    pipe = _Pipe(conversation_engine=None, tts_voice="en_GB-nova-medium")
    fake = _AssistPipeline(pipelines=[pipe])
    with _component(fake), caplog.at_level(logging.INFO, logger=bs.__name__):
        assert await bs._create_pipeline(engines) is True
    assert fake.updates == [("Nova", {"conversation_engine": "conversation.nova",
                                      "tts_voice": None})]
    assert "updated pipeline 'Nova' (conversation_engine, tts_voice)" in caplog.text
    assert "pipeline voice 'en_GB-nova-medium' is gone" in caplog.text


async def test_existing_update_error_still_counts(bs, engines, caplog):
    fake = _AssistPipeline(pipelines=[_Pipe(conversation_engine="conversation.x")],
                           update_error=lambda p: True)
    with _component(fake), caplog.at_level(logging.WARNING, logger=bs.__name__):
        assert await bs._create_pipeline(engines) is True
    assert "couldn't update existing pipeline: update refused" in caplog.text


async def test_existing_correct_pipeline_is_left_alone(bs, engines, caplog):
    fake = _AssistPipeline(pipelines=[_Pipe(conversation_engine="conversation.nova")])
    with _component(fake), caplog.at_level(logging.INFO, logger=bs.__name__):
        assert await bs._create_pipeline(engines) is True
    assert fake.updates == []
    assert "Nova pipeline already correct (agent=conversation.nova)" in caplog.text


# ── marker and version ──────────────────────────────────────────────────────

def test_unreadable_marker_reads_as_empty(bs):
    bs.MARKER_PATH.parent.mkdir(parents=True)
    bs.MARKER_PATH.write_text("{not json")
    assert bs._read_marker() == {}


def test_marker_write_failure_is_swallowed(bs, tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(bs, "MARKER_PATH", blocker / "sub" / ".bootstrap_done")
    bs._write_marker("1", {})
    assert bs._read_marker() == {}


def test_version_is_the_manifest_version(bs):
    import importlib.util
    manifest = Path(importlib.util.find_spec("jc.bootstrap").origin).parent / "manifest.json"
    assert bs._current_version() == json.loads(manifest.read_text())["version"]


# ── async_run_bootstrap ─────────────────────────────────────────────────────

class _Notices:
    def __init__(self):
        self.calls = []

    def note_voice_setup_step(self, hass, step, number, total):
        self.calls.append(("step", step, number, total))

    def note_voice_setup_incomplete(self, hass, failed):
        self.calls.append(("incomplete", list(failed)))

    def clear_voice_setup(self, hass):
        self.calls.append(("clear",))


@pytest.fixture
def run(bs, load, monkeypatch):
    """A supervised run with every phase faked and recorded."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    w = SimpleNamespace(cfg={"auto_bootstrap": True}, ensured=[], addon_ok={},
                        agent="conversation.nova", pipeline_ok=True, wyoming_error=None,
                        waits=[], created=0, real_reload=bs._reload_wyoming)
    nc = load("nova_config")
    monkeypatch.setattr(nc, "get", lambda k, d=None: w.cfg.get(k, d))
    rn = load("repair_notices")
    w.notices = _Notices()
    for name in ("note_voice_setup_step", "note_voice_setup_incomplete", "clear_voice_setup"):
        monkeypatch.setattr(rn, name, getattr(w.notices, name))

    async def _ensure(hass, slug, friendly):
        w.ensured.append((slug, friendly))
        return w.addon_ok.get(slug, True)

    async def _wyoming(hass):
        if w.wyoming_error:
            raise w.wyoming_error
        return 1

    async def _wait(hass, **kw):
        w.waits.append(kw)
        return w.agent

    async def _create(hass):
        w.created += 1
        return w.pipeline_ok
    monkeypatch.setattr(bs, "_ensure_addon", _ensure)
    monkeypatch.setattr(bs, "_reload_wyoming", _wyoming)
    monkeypatch.setattr(bs, "_wait_for_agent", _wait)
    monkeypatch.setattr(bs, "_create_pipeline", _create)
    return w


async def test_full_run_status_and_marker(bs, run, fake_hass):
    status = await bs.async_run_bootstrap(fake_hass)
    assert status == {"supervised": True, "addons_ok": True, "wyoming_ok": True,
                      "pipeline_ok": True, "skipped": None, "pipeline_tried": True}
    assert run.ensured == list(bs.REQUIRED_ADDONS.items())
    assert run.waits == [{}]
    assert bs._read_marker() == {"version": bs._current_version(), **status}


async def test_every_addon_is_tried_after_a_failure(bs, run, fake_hass):
    run.addon_ok = {"core_piper": False}
    status = await bs.async_run_bootstrap(fake_hass)
    assert [slug for slug, _ in run.ensured] == ["core_piper", "core_whisper",
                                                  "core_openwakeword"]
    assert status["addons_ok"] is False


async def test_failed_addons_rerun_next_start(bs, run, fake_hass):
    run.addon_ok = {"core_whisper": False}
    await bs.async_run_bootstrap(fake_hass)
    run.ensured.clear()
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["skipped"] is None and len(run.ensured) == 3


async def test_wyoming_error_is_named_in_the_notice(bs, run, fake_hass):
    run.wyoming_error = RuntimeError("down")
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["wyoming_ok"] is False
    assert run.notices.calls[-1] == ("incomplete", ["Wyoming"])


async def test_all_failed_parts_in_order(bs, run, fake_hass):
    run.addon_ok = {"core_piper": False}
    run.wyoming_error = RuntimeError("down")
    run.agent = None
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["pipeline_ok"] is False and run.created == 0
    assert run.notices.calls[-1] == (
        "incomplete", ["Piper, Whisper, openWakeWord", "Wyoming", "Assist pipeline"])


async def test_agent_timeout_logs_and_skips_pipeline(bs, run, fake_hass, caplog):
    run.agent = None
    with caplog.at_level(logging.WARNING, logger=bs.__name__):
        await bs.async_run_bootstrap(fake_hass)
    assert "conversation agent didn't register in time" in caplog.text


async def test_pipeline_off_is_not_tried_or_blamed(bs, run, fake_hass):
    run.cfg["auto_pipeline"] = False
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["pipeline_tried"] is False and status["pipeline_ok"] is False
    assert run.waits == [] and run.created == 0
    assert run.notices.calls[-1] == ("clear",)


async def test_no_supervisor_skip_is_logged(bs, run, fake_hass, monkeypatch, caplog):
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    with caplog.at_level(logging.INFO, logger=bs.__name__):
        status = await bs.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "no Supervisor — voice auto-setup needs HA OS/Supervised"
    assert "pipeline_tried" not in status
    assert "skipping voice-stack setup" in caplog.text


async def test_marker_of_other_version_reruns(bs, run, fake_hass):
    bs._write_marker("0.0.0", {"addons_ok": True})
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["skipped"] is None


async def test_marker_without_addons_ok_reruns(bs, run, fake_hass):
    bs._write_marker(bs._current_version(), {"addons_ok": False})
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["skipped"] is None


async def test_skip_with_good_marker_clears_the_notice(bs, run, fake_hass):
    bs._write_marker(bs._current_version(), {"addons_ok": True, "pipeline_tried": False})
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "already bootstrapped this version"
    assert run.notices.calls == [("clear",)] and run.ensured == []


async def test_skip_recheck_never_raises(bs, run, fake_hass, load, monkeypatch):
    bs._write_marker(bs._current_version(),
                     {"addons_ok": True, "pipeline_tried": True, "pipeline_ok": False})
    sh = load("setup_health")

    def _boom(hass):
        raise RuntimeError("doctor down")
    monkeypatch.setattr(sh, "_check_assist_pipeline", _boom)
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["skipped"] == "already bootstrapped this version"
    assert run.notices.calls == []


async def test_failed_wyoming_reloads_are_reported(bs, run, fake_hass, monkeypatch):
    """Fixed (bugs.md #19): a failing reload marks Wyoming as not ok."""
    monkeypatch.setattr(bs, "_reload_wyoming", run.real_reload)
    fake_hass.config_entries = _Entries(["w1", "w2"], failing={"w2"})
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["wyoming_ok"] is False
    assert run.notices.calls[-1] != ("clear",)


async def test_non_object_marker_reads_as_no_marker(bs, run, fake_hass):
    """Fixed (bugs.md #20): a marker file holding valid JSON that is not an
    object reads as no marker, so the run goes ahead."""
    bs.MARKER_PATH.parent.mkdir(parents=True)
    bs.MARKER_PATH.write_text("[1, 2]")
    assert bs._read_marker() == {}
    status = await bs.async_run_bootstrap(fake_hass)
    assert status["addons_ok"] is True and not status.get("skipped")


# ── async_ensure_pipeline_agent ─────────────────────────────────────────────

@pytest.fixture
def agent_wait(bs, monkeypatch):
    seen = SimpleNamespace(agent="conversation.nova", kwargs=[])

    async def _wait(hass, **kw):
        seen.kwargs.append(kw)
        return seen.agent
    monkeypatch.setattr(bs, "_wait_for_agent", _wait)
    return seen


async def test_repair_without_component_does_nothing(bs, agent_wait, fake_hass):
    assert await bs.async_ensure_pipeline_agent(fake_hass) is None
    assert agent_wait.kwargs == []


async def test_repair_waits_twenty_tries_three_seconds(bs, agent_wait, fake_hass):
    fake = _AssistPipeline()
    with _component(fake):
        await bs.async_ensure_pipeline_agent(fake_hass)
    assert agent_wait.kwargs == [{"tries": 20, "delay": 3.0}]


async def test_repair_without_agent_touches_nothing(bs, agent_wait, fake_hass):
    agent_wait.agent = None
    fake = _AssistPipeline(pipelines=[_Pipe(conversation_engine="conversation.x")])
    with _component(fake):
        await bs.async_ensure_pipeline_agent(fake_hass)
    assert fake.updates == []


async def test_repair_listing_error_is_quiet(bs, agent_wait, fake_hass):
    with _component(_AssistPipeline(list_error=RuntimeError("x"))):
        assert await bs.async_ensure_pipeline_agent(fake_hass) is None


async def test_repair_matches_name_or_voice_and_skips_the_rest(bs, agent_wait, fake_hass,
                                                               caplog):
    pipes = [
        _Pipe(name="Home", conversation_engine="conversation.home"),
        _Pipe(name="Kitchen", conversation_engine="conversation.home",
              tts_voice="en_GB-Nova-high"),
        _Pipe(name="NOVA", conversation_engine="conversation.nova"),
        _Pipe(name="nova two", conversation_engine=None, tts_voice=None),
    ]
    fake = _AssistPipeline(pipelines=pipes)
    with _component(fake), caplog.at_level(logging.INFO, logger=bs.__name__):
        await bs.async_ensure_pipeline_agent(fake_hass)
    assert fake.updates == [("Kitchen", {"conversation_engine": "conversation.nova"}),
                            ("nova two", {"conversation_engine": "conversation.nova"})]
    assert pipes[0].conversation_engine == "conversation.home"
    assert ("Nova: set pipeline 'Kitchen' conversation agent -> conversation.nova "
            "(was conversation.home)") in caplog.text   # fixed (bugs.md #21)


async def test_repair_error_moves_on_to_the_next(bs, agent_wait, fake_hass, caplog):
    pipes = [_Pipe(name="Nova A"), _Pipe(name="Nova B")]
    fake = _AssistPipeline(pipelines=pipes, update_error=lambda p: p.name == "Nova A")
    with _component(fake), caplog.at_level(logging.WARNING, logger=bs.__name__):
        await bs.async_ensure_pipeline_agent(fake_hass)
    assert fake.updates == [("Nova B", {"conversation_engine": "conversation.nova"})]
    assert "Nova: couldn't set pipeline 'Nova A' conversation agent: update refused" in caplog.text


# ── background runner ───────────────────────────────────────────────────────

@pytest.fixture
def runner(bs, load, monkeypatch):
    """schedule_bootstrap on a running hass; the collected task is awaited
    by the test. Each phase is recorded and can be made to raise."""
    w = SimpleNamespace(order=[], fail=set())

    def _phase(name):
        async def _run(hass, **kw):
            w.order.append(name)
            if name in w.fail:
                raise RuntimeError(name)
        return _run
    monkeypatch.setattr(bs, "async_ensure_pipeline_agent", _phase("repair"))
    monkeypatch.setattr(bs, "async_run_bootstrap", _phase("bootstrap"))
    welcome = load("welcome")
    monkeypatch.setattr(welcome, "async_maybe_show", _phase("welcome"))
    return w


def _scheduled(bs):
    hass = FakeHass()
    hass.is_running = True
    handle = bs.schedule_bootstrap(hass)
    assert len(hass._tasks) == 1
    return hass, handle


async def test_runner_order_when_supervised(bs, runner, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    hass, _ = _scheduled(bs)
    await hass.drain()
    assert runner.order == ["repair", "bootstrap", "welcome"]


async def test_runner_skips_bootstrap_without_supervisor(bs, runner, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    hass, _ = _scheduled(bs)
    await hass.drain()
    assert runner.order == ["repair", "welcome"]


async def test_runner_survives_every_phase_failing(bs, runner, monkeypatch, caplog):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    runner.fail = {"repair", "bootstrap", "welcome"}
    hass, _ = _scheduled(bs)
    with caplog.at_level(logging.WARNING, logger=bs.__name__):
        await hass.drain()
    assert runner.order == ["repair", "bootstrap", "welcome"]
    assert "Nova bootstrap: unexpected error: bootstrap" in caplog.text


# ── handle ──────────────────────────────────────────────────────────────────

def test_fresh_handle_is_empty_and_shutdown_is_safe(bs):
    handle = bs.BootstrapHandle()
    assert (handle.unsub_start, handle.task, handle.closed) == (None, None, False)
    handle.shutdown()
    assert handle.closed is True


def test_shutdown_swallows_a_failing_remover(bs):
    handle = bs.BootstrapHandle()

    def _boom():
        raise RuntimeError("already gone")
    handle.unsub_start = _boom
    handle.shutdown()
    assert handle.unsub_start is None and handle.closed is True


def test_shutdown_leaves_a_finished_task(bs):
    task = SimpleNamespace(done=lambda: True, cancelled=False)
    task.cancel = lambda: setattr(task, "cancelled", True)
    handle = bs.BootstrapHandle()
    handle.task = task
    handle.shutdown()
    assert task.cancelled is False and handle.task is None
