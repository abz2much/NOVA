"""Service health rows and their roll-up (diagnostics/service_health.py).

Every row answers "is this piece of software Nova calls working?". The rule
under test throughout: a missed synthetic poke reads IDLE at worst, and only
a failure reported from real use (record_usage) may turn a row DOWN.

Each test gets its own usage record, settings and clock, so nothing leaks
between tests that share the loaded module.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass


class Clock:
    """A hand-wound stand-in for the module's `time` import."""

    def __init__(self, start=50_000.0):
        self.now = start

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def health(load, monkeypatch):
    mod = load("diagnostics.service_health")
    monkeypatch.setattr(mod, "_USAGE", {})
    settings: dict = {}
    monkeypatch.setattr(mod, "_cfg", lambda key, default=None: settings.get(key, default))
    clock = Clock()
    monkeypatch.setattr(mod, "time", clock)
    pauses: list = []

    async def _pause(seconds):
        pauses.append(seconds)
    monkeypatch.setattr(mod, "asyncio", types.SimpleNamespace(sleep=_pause))
    mod.test_settings, mod.test_clock, mod.test_pauses = settings, clock, pauses
    return mod


def home_with(**domains):
    """A FakeHass holding entities per domain, e.g. tts=[("tts.piper", "idle")]."""
    hass = FakeHass()
    for rows in domains.values():
        for entity_id, state in rows:
            hass.states.set(entity_id, state)
    return hass


def stub_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    mod.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return mod


# ── credential stripping ────────────────────────────────────────────────────

@pytest.mark.parametrize("given, shown", [
    ("http://user:pass@host:11434", "http://host:11434"),
    ("https://token-only@ollama.lan/v1", "https://ollama.lan/v1"),
    ("http://gpu.local:11434", "http://gpu.local:11434"),
    ("", ""),
])
def test_urls_lose_their_credentials(health, given, shown):
    assert health._redact(given) == shown


# ── record of real use ──────────────────────────────────────────────────────

def test_nothing_recorded_is_neutral(health):
    assert health._recent_real_failure("never-used") is None
    assert health._recently_used_ok("never-used") is False


def test_a_success_counts_as_recent_use(health):
    health.record_usage("embeddings", True)
    assert health._recently_used_ok("embeddings") is True
    assert health._recent_real_failure("embeddings") is None


def test_a_failure_keeps_its_reason_or_a_default(health):
    health.record_usage("embeddings", False, "boom")
    assert health._recent_real_failure("embeddings") == "boom"
    health.record_usage("tts", False)
    assert health._recent_real_failure("tts") == "a real request failed"


def test_a_later_success_wipes_the_failure(health):
    health.record_usage("embeddings", False, "boom")
    health.test_clock.advance(1)
    health.record_usage("embeddings", True)
    assert health._recent_real_failure("embeddings") is None
    assert "fail_detail" not in health._USAGE["embeddings"]


@pytest.mark.parametrize("extra, still_failing", [(0, True), (1, False), (10, False)])
def test_a_failure_stops_alarming_after_its_window(health, extra, still_failing):
    health.record_usage("stt", False, "old failure")
    health.test_clock.advance(health._REAL_FAIL_TTL + extra)
    assert (health._recent_real_failure("stt") is not None) is still_failing


@pytest.mark.parametrize("extra, recent", [(0, True), (1, False), (10, False)])
def test_recent_use_expires_after_its_window(health, extra, recent):
    health.record_usage("llm", True)
    health.test_clock.advance(health._RECENT_USE_TTL + extra)
    assert health._recently_used_ok("llm") is recent


# ── retrying a cold service ─────────────────────────────────────────────────

def scripted(*results):
    """An async callable handing out `results` in turn (an exception raises)."""
    queue = list(results)
    calls = []

    async def _call():
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    _call.calls = calls
    return _call


async def test_retry_stops_at_the_first_success(health):
    probe = scripted({"ok": False}, {"ok": True}, {"ok": False})
    assert await health._retry_async(probe, attempts=3, delay=0.5) == {"ok": True}
    assert len(probe.calls) == 2
    assert health.test_pauses == [0.5]


async def test_retry_hands_back_the_last_miss(health):
    probe = scripted({"ok": False, "error": "warming"}, {"ok": False, "error": "still cold"})
    out = await health._retry_async(probe, attempts=2, delay=0)
    assert out == {"ok": False, "error": "still cold"}
    assert len(probe.calls) == 2 and health.test_pauses == [0]


async def test_retry_turns_an_exception_into_a_miss(health):
    probe = scripted(RuntimeError("refused"))
    assert await health._retry_async(probe, attempts=1) == {"ok": False, "error": "refused"}
    assert health.test_pauses == []


# ── TTS and STT ─────────────────────────────────────────────────────────────

def speech(health, hass, domain="tts", configured="auto"):
    return health._check_speech_entity(hass, domain, domain.upper(), configured)


def test_no_engine_registered_is_a_calm_setup_gap(health):
    row = speech(health, home_with())
    assert row["status"] == "idle"
    assert row["detail"] == "no tts engine registered (install the add-on to enable)"


def test_no_engine_but_recent_success_reads_ok(health):
    health.record_usage("stt", True)
    row = speech(health, home_with(), "stt")
    assert (row["status"], row["detail"]) == ("ok", "stt used successfully recently")


def test_auto_picks_the_first_awake_engine(health):
    hass = home_with(stt=[("stt.sleepy", "unavailable"), ("stt.whisper", "idle")])
    row = speech(health, hass, "stt")
    assert row["status"] == "ok" and row["entity"] == "stt.whisper"
    assert row["detail"] == "1 engine(s) available (auto)"


@pytest.mark.parametrize("asleep", ["unavailable", "unknown"])
def test_auto_with_every_engine_asleep_is_idle(health, asleep):
    row = speech(health, home_with(tts=[("tts.piper", asleep)]))
    assert row["status"] == "idle" and "entity" not in row
    assert row["detail"].startswith("1 engine(s) present, idle")


def test_auto_asleep_but_recently_used_reads_ok(health):
    health.record_usage("stt", True)
    row = speech(health, home_with(stt=[("stt.whisper", "unavailable")]), "stt")
    assert row["status"] == "ok"
    assert row["detail"] == "1 engine(s) present, idle (recent stt succeeded)"


def test_a_real_failure_is_down_whatever_the_engines_say(health):
    health.record_usage("stt", False, "transcription timed out")
    row = speech(health, home_with(stt=[("stt.whisper", "idle")]), "stt")
    assert row["status"] == "down"
    assert row["detail"] == "a real stt request failed: transcription timed out"


def test_named_engine_found_and_awake(health):
    hass = home_with(tts=[("tts.piper", "idle"), ("tts.cloud", "idle")])
    row = speech(health, hass, configured="tts.piper")
    assert (row["status"], row["entity"]) == ("ok", "tts.piper")
    assert row["detail"] == "tts.piper available"


def test_named_engine_missing_warns_and_counts_the_rest(health):
    row = speech(health, home_with(tts=[("tts.cloud", "idle")]), configured="tts.piper")
    assert row["status"] == "warn"
    assert row["detail"] == "configured 'tts.piper' not found; 1 other(s) present"


def test_named_engine_asleep_is_idle_not_down(health):
    row = speech(health, home_with(stt=[("stt.whisper", "unavailable")]), "stt", "stt.whisper")
    assert row["status"] == "idle" and row["entity"] == "stt.whisper"


# ── LLM ─────────────────────────────────────────────────────────────────────

async def test_llm_real_failure_shows_the_reason(health):
    health.test_settings["llm_provider"] = "groq"
    health.record_usage("llm", False, "model 'llama-x' not found on provider 'groq'")
    row = await health._check_llm(FakeHass())
    assert row["status"] == "down"
    assert row["detail"] == "a real agent call failed: model 'llama-x' not found on provider 'groq'"


# ── embeddings ──────────────────────────────────────────────────────────────

def embedder(monkeypatch, *answers):
    probe = scripted(*answers)

    async def _probe(hass):
        return await probe()
    stub_module(monkeypatch, "embeddings", is_enabled=lambda: True, probe=_probe)
    return probe


async def test_embeddings_probe_that_keeps_missing_is_idle(health, monkeypatch):
    probe = embedder(monkeypatch, *([{"ok": False, "error": "cold"}] * 3))
    row = await health._check_embeddings(FakeHass())
    assert row["status"] == "idle"
    assert len(probe.calls) == 3          # retried, with no real waiting
    assert health.test_pauses == [1.2, 1.2]


async def test_embeddings_miss_covered_by_recent_real_use(health, monkeypatch):
    health.record_usage("embeddings", True)
    embedder(monkeypatch, *([{"ok": False, "error": "model idle"}] * 3))
    row = await health._check_embeddings(FakeHass())
    assert row["status"] == "ok" and row["detail"].startswith("working")


async def test_embeddings_real_failure_beats_a_good_probe(health, monkeypatch):
    health.record_usage("embeddings", False, "ingest failed")
    probe = embedder(monkeypatch, {"ok": True, "model": "nomic", "dim": 768})
    row = await health._check_embeddings(FakeHass())
    assert row["status"] == "down"
    assert row["detail"] == "a real embedding request failed: ingest failed"
    assert probe.calls == []              # the probe is not even asked


# ── cameras ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cameras, status, detail", [
    ([], "off", "no cameras configured (optional)"),
    ([("camera.front", "idle"), ("camera.back", "streaming")], "ok", "2 camera(s) available"),
    ([("camera.front", "idle"), ("camera.back", "unavailable")],
     "warn", "1/2 available, 1 unavailable"),
    ([("camera.front", "unavailable")], "warn", "all 1 camera(s) unavailable"),
    ([("camera.front", "unknown"), ("camera.back", "unavailable")],
     "warn", "all 2 camera(s) unavailable"),
])
def test_camera_rows(health, cameras, status, detail):
    row = health._check_cameras(home_with(camera=cameras))
    assert (row["status"], row["detail"]) == (status, detail)


# ── routine attribution setting ─────────────────────────────────────────────

@pytest.mark.parametrize("bar, status", [(0.95, "warn"), (0.85, "warn"),
                                         (0.84, "ok"), (0.5, "ok")])
def test_routines_flag_a_very_high_identity_bar(health, bar, status):
    health.test_settings["identity_min_confidence"] = bar
    row = health._check_routines(None)
    assert row["status"] == status
    assert f"identity confidence {bar:.2f}" in row["detail"]


# ── conversation store ──────────────────────────────────────────────────────

def test_store_not_yet_created_is_off(health):
    # The redirected test /config holds no database, so this is the real
    # module answering, never a false DOWN.
    row = health._check_database(FakeHass())
    assert (row["status"], row["detail"]) == ("off", "not yet created")


def present_store(monkeypatch, report):
    def _health():
        if isinstance(report, BaseException):
            raise report
        return report
    stub_module(monkeypatch, "database",
                _db_path=lambda: types.SimpleNamespace(exists=lambda: True),
                health=_health)


@pytest.mark.parametrize("report, status, detail", [
    ({"ok": True, "error": ""}, "ok", "conversation store OK"),
    ({"ok": False, "error": "disk I/O error"}, "down", "schema/migration error: disk I/O error"),
    ({"ok": False}, "down", "schema/migration error: unknown"),
    (OSError("locked"), "down", "health probe error: locked"),
])
def test_existing_store_probe_outcomes(health, monkeypatch, report, status, detail):
    present_store(monkeypatch, report)
    row = health._check_database(FakeHass())
    assert (row["status"], row["detail"]) == (status, detail)


# ── scheduler (read from the entry's runtime) ───────────────────────────────

class TaskBoard:
    def __init__(self, tasks):
        self.tasks = tasks

    def status(self):
        return self.tasks


def test_scheduler_off_without_a_loaded_entry(health):
    assert health._check_scheduler(types.SimpleNamespace(data={"nova": {}}))["status"] == "off"


def test_scheduler_in_the_old_hass_data_bridge_is_ignored(health):
    board = TaskBoard([{"name": "health", "consecutive_errors": 0}])
    hass = types.SimpleNamespace(data={"nova": {"e1": {"scheduler": board}}})
    assert health._check_scheduler(hass)["status"] == "off"


@pytest.mark.parametrize("tasks, status, detail", [
    ([{"name": "package", "consecutive_errors": 0}, {"name": "health", "consecutive_errors": 0}],
     "ok", "2 periodic task(s) scheduled"),
    ([{"name": "docs", "consecutive_errors": 2}], "ok", "1 periodic task(s) scheduled"),
    ([{"name": "hazard", "consecutive_errors": 5}, {"name": "docs", "consecutive_errors": 0}],
     "warn", "1 task(s) failing: hazard"),
    ([{"name": "hazard", "consecutive_errors": 3}], "warn", "1 task(s) failing: hazard"),
])
def test_scheduler_rows_from_the_runtime(health, nova_runtime, tasks, status, detail):
    hass = types.SimpleNamespace(data={"nova": {}})
    nova_runtime(hass, scheduler=TaskBoard(tasks))
    row = health._check_scheduler(hass)
    assert (row["status"], row["detail"]) == (status, detail)


# ── Voice ID (read from the entry's runtime) ────────────────────────────────

def voice_home(nova_runtime, status):
    hass = types.SimpleNamespace(data={"nova": {}})
    app = None if status is None else types.SimpleNamespace(status=lambda: status)
    nova_runtime(hass, voice_id=app)
    return hass


@pytest.mark.parametrize("status, expected", [
    (None, "off"),
    ({"state": "not_set"}, "off"),
    ({"state": "connected", "version": "0.1.0"}, "ok"),
    ({"state": "unreachable"}, "idle"),
    ({"state": "checking"}, "idle"),
])
def test_voice_id_row(health, nova_runtime, status, expected):
    row = health._check_voice_id(voice_home(nova_runtime, status))
    assert row["key"] == "voice_id" and row["status"] == expected


def test_voice_id_real_failure_is_down(health, nova_runtime):
    health.record_usage("voice_id", False, "app unreachable; used fallback")
    row = health._check_voice_id(voice_home(nova_runtime, {"state": "connected"}))
    assert row["status"] == "down"
    assert row["detail"] == "a real request failed: app unreachable; used fallback"


def test_voice_id_without_any_entry_is_off(health):
    row = health._check_voice_id(FakeHass())
    assert (row["status"], row["detail"]) == ("off", "no app address set")


# ── the roll-up ─────────────────────────────────────────────────────────────

def canned(monkeypatch, health, keep=(), **statuses):
    """Swap every check (bar those in `keep`) for one returning a fixed row;
    unnamed keys are OFF."""
    for name, key, fn_name in health._CHECKS:
        if key in keep:
            continue
        row = {"name": name, "key": key, "status": statuses.get(key, "off"), "detail": ""}
        if key in health._AWAITED:
            async def check(hass, _row=row):
                return dict(_row)
        else:
            def check(hass, _row=row):
                return dict(_row)
        monkeypatch.setattr(health, fn_name, check)


@pytest.mark.parametrize("statuses, overall, summary", [
    (dict(llm="ok", tts="down", stt="ok"), "down", "1 service(s) failing — 2/3 healthy"),
    (dict(llm="ok", tts="ok", stt="ok"), "ok", "all 3 core services healthy"),
    (dict(llm="ok", embeddings="idle", tts="idle", stt="ok"),
     "ok", "2/4 healthy, 2 idle (not recently used)"),
    (dict(llm="ok", cameras="warn"), "warn", "1 service(s) need a look — 1/2 healthy"),
    (dict(llm="down", cameras="warn"), "down", "1 service(s) failing — 0/2 healthy"),
    ({}, "off", "no core services active"),
])
async def test_rollup_overall_and_summary(health, monkeypatch, statuses, overall, summary):
    canned(monkeypatch, health, **statuses)
    report = await health.run_service_health(FakeHass())
    assert report["overall"] == overall
    assert report["summary"] == summary
    assert [row["key"] for row in report["services"]] == [k for _, k, _ in health._CHECKS]
    assert len(report["services"]) == 10


async def test_a_broken_check_becomes_a_down_row_not_a_crash(health, monkeypatch):
    canned(monkeypatch, health, stt="ok")

    async def _explode(hass):
        raise RuntimeError("kaboom")

    def _explode_now(hass):
        raise RuntimeError("x")
    monkeypatch.setattr(health, "_check_llm", _explode)
    monkeypatch.setattr(health, "_check_embeddings", _explode)
    monkeypatch.setattr(health, "_check_tts", _explode_now)
    report = await health.run_service_health(FakeHass())
    rows = {row["key"]: row for row in report["services"]}
    assert len(rows) == 10
    assert rows["llm"] == {"name": "LLM", "key": "llm", "status": "down", "detail": "kaboom"}
    assert rows["embeddings"]["detail"] == "kaboom"
    assert rows["tts"] == {"name": "TTS", "key": "tts", "status": "down", "detail": "x"}
    assert report["overall"] == "down"


async def test_rollup_runs_the_real_camera_check(health, monkeypatch):
    canned(monkeypatch, health, keep=("cameras",))
    report = await health.run_service_health(home_with(camera=[("camera.front", "idle")]))
    rows = {row["key"]: row for row in report["services"]}
    assert rows["cameras"]["status"] == "ok"
    assert report["summary"] == "all 1 core services healthy"


# ── agent wiring ────────────────────────────────────────────────────────────

def test_agent_exposes_the_diagnostics_tool(load):
    agent = load("agent")
    assert "system_diagnostics" in {tool["function"]["name"] for tool in agent.NOVA_TOOLS}
    assert "system_diagnostics" in agent._TOOL_MAP
