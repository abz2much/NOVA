"""Scheduled morning and evening briefings.

The scheduler sits in the integration's __init__.py, which cannot be
imported without setting up the whole integration. So the scheduling
functions are lifted out of its source with ast and run against small
stand-ins, and the wiring around them (panel switches, read-back, the
announce helpers) is checked in the source text.
"""
import __future__
import ast
import logging
import re
import sys
import types

import pytest
from ws_sources import COMP, panel_config_keys, panel_writable_keys

INIT_PATH = COMP / "__init__.py"


def source(name):
    return (COMP / name).read_text(encoding="utf-8")


INIT = source("__init__.py")
BRIEFING = source("briefing.py")
SERVICES = source("services.py")
PANEL = source("frontend/nova-panel.js")

SCHEDULER = ("_ON_WORDS", "_briefing_setting", "_briefing_on", "_parse_hhmm",
             "_house_empty", "_run_briefing", "_morning_briefing",
             "_evening_briefing", "_schedule_briefings")
SWITCHES = ("briefing_morning_enabled", "briefing_evening_enabled",
            "briefing_require_home", "briefing_include_hazards")
TIMES = ("briefing_morning_time", "briefing_evening_time")


def scheduler_block() -> str:
    """The setup-scope scheduler section of __init__.py."""
    after = INIT.split("Scheduled briefings", 1)[1]
    return after.split("async def _async_update_listener", 1)[0]


def lift(**stand_ins) -> dict:
    """The scheduler's functions (and _ON_WORDS) compiled on their own, with
    `stand_ins` as the names they would have imported."""
    wanted = []
    for node in ast.parse(INIT, filename=str(INIT_PATH)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            named = node.name
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            named = node.targets[0].id
        else:
            continue
        if named in SCHEDULER:
            wanted.append(node)
    assert len(wanted) == len(SCHEDULER), "a scheduler function moved or was renamed"
    code = compile(ast.Module(body=wanted, type_ignores=[]), str(INIT_PATH), "exec",
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    space = {"__name__": "jc.briefing_probe", "__package__": "jc",
             "SimpleNamespace": types.SimpleNamespace,
             "_LOGGER": logging.getLogger("nova.briefing_probe")}
    space.update(stand_ins)
    exec(code, space)
    return space


@pytest.fixture
def settings(monkeypatch):
    """The panel's settings as nova_config.get reads them."""
    held: dict = {}
    fake = types.ModuleType("jc.nova_config")
    fake.get = lambda key, default=None: held.get(key, default)
    monkeypatch.setitem(sys.modules, "jc.nova_config", fake)
    return held


@pytest.fixture
def presence(monkeypatch):
    """presence["away"] is what household.everyone_away answers; an
    exception instance there is raised instead."""
    answer = {"away": False}

    def everyone_away(hass):
        if isinstance(answer["away"], Exception):
            raise answer["away"]
        return answer["away"]
    fake = types.ModuleType("jc.household")
    fake.everyone_away = everyone_away
    monkeypatch.setitem(sys.modules, "jc.household", fake)
    return answer


@pytest.fixture
def sched(settings, presence):
    """The lifted scheduler. Briefings it delivers land in sched["delivered"]
    as the call data, time registrations in sched["registered"]; an error put
    in sched["failure"]["error"] makes the briefing engine raise it."""
    delivered, registered, failure = [], [], {}

    async def async_briefing(hass, call, client, honorific, tts, speakers):
        if failure:
            raise failure["error"]
        delivered.append(dict(call.data, _who=(honorific, tts, tuple(speakers))))

    def async_track_time_change(hass, job, **when):
        registered.append((when, job))
        return f"unsub-{len(registered)}"

    from functools import partial
    space = lift(async_briefing=async_briefing,
                 async_track_time_change=async_track_time_change, partial=partial,
                 _live_honorific=lambda hass: "boss",
                 _get_tts=lambda hass, entry, context="chat": "tts.cloud",
                 _get_speakers=lambda hass, entry: ["media_player.kitchen"])
    space.update(delivered=delivered, registered=registered, failure=failure)
    return space


# ── reading the times and switches ──────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("07:30", (7, 30)), ("6:05", (6, 5)), ("23:59:10", (23, 59)), ("00:00", (0, 0)),
    ("24:00", (9, 15)), ("12:60", (9, 15)), ("-1:30", (9, 15)), ("08", (9, 15)),
    ("noon", (9, 15)), ("", (9, 15)), (None, (9, 15)),
])
def test_time_of_day_parsing(sched, raw, expected):
    assert sched["_parse_hhmm"](raw, 9, 15) == expected


@pytest.mark.parametrize("stored, expected", [
    (True, True), (False, False), ("on", True), (" Yes ", True), ("1", True),
    ("off", False), ("false", False), ("0", False), ("maybe", False),
])
def test_switch_reading(sched, settings, stored, expected):
    settings["briefing_morning_enabled"] = stored
    assert sched["_briefing_on"]("briefing_morning_enabled", not expected) is expected


@pytest.mark.parametrize("default", [True, False])
def test_unset_or_null_switch_uses_the_default(sched, settings, default):
    assert sched["_briefing_on"]("briefing_evening_enabled", default) is default
    settings["briefing_evening_enabled"] = None
    assert sched["_briefing_on"]("briefing_evening_enabled", default) is default


# ── when a scheduled briefing runs ──────────────────────────────────────────

async def test_briefings_are_off_until_switched_on(sched, fake_hass):
    # Opt-in: Nova must not start talking on a schedule unannounced.
    await sched["_morning_briefing"](fake_hass, object(), "client", None)
    await sched["_evening_briefing"](fake_hass, object(), "client", None)
    assert sched["delivered"] == []
    assert 'f"briefing_{kind}_enabled", False' in INIT


async def test_morning_and_evening_cover_different_spans(sched, settings, fake_hass):
    settings.update(briefing_morning_enabled=True, briefing_evening_enabled="on")
    await sched["_morning_briefing"](fake_hass, object(), "client", None)
    await sched["_evening_briefing"](fake_hass, object(), "client", None)
    morning, evening = sched["delivered"]
    assert (morning["hours"], evening["hours"]) == (12, 14)
    assert morning["announce"] is True
    assert morning["_who"] == ("boss", "tts.cloud", ("media_player.kitchen",))


async def test_each_section_follows_its_switch(sched, settings, fake_hass):
    settings.update(briefing_morning_enabled=True, briefing_include_energy="off",
                    briefing_include_hazards=False)
    await sched["_morning_briefing"](fake_hass, object(), "client", None)
    (sent,) = sched["delivered"]
    sections = {k[len("include_"):]: v for k, v in sent.items() if k.startswith("include_")}
    assert sections == {"weather": True, "calendar": True, "presence": True,
                        "events": True, "energy": False, "hazards": False}


@pytest.mark.parametrize("require_home, away, delivered", [
    (None, True, 0),                     # default: skip an empty house
    (True, False, 1),
    (True, None, 1),                     # unknown presence is never away
    (True, RuntimeError("no people"), 1),  # a failing check fails open
    (False, True, 1),                    # switched off: brief anyway
])
async def test_presence_gate(sched, settings, presence, fake_hass,
                             require_home, away, delivered):
    settings["briefing_morning_enabled"] = True
    if require_home is not None:
        settings["briefing_require_home"] = require_home
    presence["away"] = away
    await sched["_morning_briefing"](fake_hass, object(), "client", None)
    assert len(sched["delivered"]) == delivered


async def test_a_failed_briefing_is_logged_as_a_warning(sched, settings, fake_hass, caplog):
    settings["briefing_evening_enabled"] = True
    sched["failure"]["error"] = RuntimeError("model offline")
    with caplog.at_level(logging.DEBUG, logger="nova.briefing_probe"):
        await sched["_evening_briefing"](fake_hass, object(), "client", None)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warned] == ["Nova evening briefing failed: model offline"]


# ── when they are scheduled ─────────────────────────────────────────────────

def test_default_clock_times(sched, fake_hass):
    unsubs = []
    sched["_schedule_briefings"](fake_hass, object(), "client", unsubs)
    when = [w for w, _job in sched["registered"]]
    assert when == [{"hour": 7, "minute": 30, "second": 0},
                    {"hour": 19, "minute": 30, "second": 0}]
    assert unsubs == ["unsub-1", "unsub-2"]


def test_configured_times_and_a_bad_one(sched, settings, fake_hass):
    settings.update(briefing_morning_time="06:45", briefing_evening_time="25:00")
    sched["_schedule_briefings"](fake_hass, object(), "client", [])
    when = [(w["hour"], w["minute"]) for w, _job in sched["registered"]]
    assert when == [(6, 45), (19, 30)]


async def test_the_registered_jobs_run_their_own_briefing(sched, settings, fake_hass):
    settings.update(briefing_morning_enabled=True, briefing_evening_enabled=True)
    sched["_schedule_briefings"](fake_hass, object(), "client", [])
    morning_job, evening_job = (job for _w, job in sched["registered"])
    await evening_job("fired-at")
    await morning_job("fired-at")
    assert [d["hours"] for d in sched["delivered"]] == [14, 12]


# ── the same rules, as written in the source ────────────────────────────────

def test_scheduler_uses_wall_clock_times_and_presence_helper():
    for needle in ("async_track_time_change", "_morning_briefing", "_evening_briefing",
                   "_parse_hhmm", '"07:30"', '"19:30"', "briefing_require_home",
                   "household.everyone_away(hass)"):
        assert needle in INIT, needle


def test_scheduler_passes_the_real_client_name():
    """A stale client name once raised NameError on every scheduled run,
    swallowed by the handler (6.78.1)."""
    assert "async_briefing(hass, call, llm_client," in INIT
    block = scheduler_block()
    assert "groq_client" not in block
    assert "_LOGGER.warning" in block and "briefing failed" in block


def test_briefing_content_has_hazards_and_a_plain_fallback():
    for needle in ("include_hazards", "Active hazards nearby", "hazard_monitor",
                   "_plain_briefing", "empty model output"):
        assert needle in BRIEFING, needle


# ── the panel ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", SWITCHES + TIMES)
def test_setting_is_writable_from_the_panel(key):
    assert key in panel_writable_keys()


@pytest.mark.parametrize("key", SWITCHES[:2] + TIMES)
def test_setting_is_read_back_by_the_panel(key):
    # A control the panel shows must come back from get_panel_data, or it
    # resets on the next render.
    assert key in panel_config_keys()


@pytest.mark.parametrize("key", SWITCHES)
def test_panel_toggle_is_wired(key):
    # The panel builds its toggles through a shared helper, so the evidence
    # is the helper call carrying the key.
    assert re.search(r'(onOff|feedChip)\("' + key + r'"', PANEL), f"{key} toggle is not wired"


def test_panel_has_a_brief_me_now_button():
    assert "newBriefNow" in PANEL


# ── announcing ──────────────────────────────────────────────────────────────

def test_hazard_monitor_speaks_the_spoken_text():
    # async_announce is (hass, text, tts_entity, speakers); the hazard monitor
    # once passed them out of order. It now speaks through delivery.py.
    assert "spoken=speak_text" in source("hazard_monitor.py")


def test_no_announce_call_puts_a_speaker_where_the_text_goes():
    call = re.compile(r"async_announce\(\s*hass,\s*([A-Za-z_][\w\.\[\]\"']*)")
    wrong = [f"{path.name}: async_announce(hass, {m.group(1)}, ...)"
             for path in sorted(COMP.glob("*.py"))
             for m in call.finditer(path.read_text(encoding="utf-8"))
             if m.group(1) in {"tts", "tts_entity", "spk", "speakers"}]
    assert wrong == [], f"wrong async_announce argument order: {wrong}"


def test_briefing_voice_and_speakers_come_from_the_effective_config():
    # entry.options is empty in practice; reading it made the briefing go
    # quiet before reaching TTS (6.96.0).
    tts_at, spk_at = SERVICES.index("def _get_tts"), SERVICES.index("def _get_speakers")
    tts_body, spk_body = SERVICES[tts_at:spk_at], SERVICES[spk_at:spk_at + 1400]
    assert "effective_config" in tts_body, "_get_tts must use effective_config"
    assert "effective_config" in spk_body, "_get_speakers must use effective_config"
    assert "entry.options.get(CONF_TTS_ENGINE" not in tts_body
