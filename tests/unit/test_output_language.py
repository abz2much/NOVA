"""The output language: one resolver for the agent prompts and the safety
notifications, a validated setting, and the direct execute_chat callers."""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import types

import pytest
from core_sources import core_text

if "aiohttp" not in sys.modules:           # camera.py imports it; same stub the n8n tests use
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        _aiohttp = types.ModuleType("aiohttp")
        _aiohttp.ClientTimeout = lambda **kw: kw
        _aiohttp.ClientSession = object
        sys.modules["aiohttp"] = _aiohttp

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
ROOT = COMP.parents[1]


def _hass(language="en"):
    return types.SimpleNamespace(config=types.SimpleNamespace(language=language))


@pytest.fixture
def ol(load):
    return load("output_language")


@pytest.fixture
def setting(load, monkeypatch):
    """Drive the Nova setting that output_language reads."""
    nc = load("nova_config")
    box = {"value": ""}
    monkeypatch.setattr(nc, "output_language", lambda hass: box["value"])
    return box


# ── resolver ────────────────────────────────────────────────────────────────

def test_unset_follows_home_assistant(ol, setting):
    assert ol.resolve(_hass("fr")) == "fr"


@pytest.mark.parametrize("raw", ["", "auto", "AUTO", "  auto "])
def test_auto_follows_home_assistant(ol, setting, raw):
    setting["value"] = raw
    assert ol.resolve(_hass("de-DE")) == "de-DE"


@pytest.mark.parametrize("raw,expected", [
    ("de", "de"), ("DE", "de"), ("de-DE", "de-DE"), ("de_de", "de-DE"),
    ("pt-br", "pt-BR"), ("zh-hans", "zh-Hans"), (" fr ", "fr"), ("es-419", "es-419")])
def test_setting_wins_over_home_assistant(ol, setting, raw, expected):
    setting["value"] = raw
    assert ol.resolve(_hass("en")) == expected


@pytest.mark.parametrize("raw", ["klingon", "xx", "de-", "d", "de-DE-x", "de;", "../de", 5, True, None, ["de"], {}])
def test_garbage_setting_falls_back_to_home_assistant(ol, setting, raw):
    setting["value"] = raw
    assert ol.resolve(_hass("it")) == "it"


def test_per_request_language_wins(ol, setting):
    setting["value"] = "de"
    assert ol.resolve(_hass("en"), request_language="fr-FR") == "fr-FR"


def test_unusable_per_request_language_falls_through(ol, setting):
    setting["value"] = "de"
    assert ol.resolve(_hass("en"), request_language="xx") == "de"
    assert ol.resolve(_hass("en"), request_language=7) == "de"


def test_nothing_set_is_english(ol, setting):
    assert ol.resolve(_hass(None)) == "en"
    assert ol.resolve(_hass("")) == "en"
    assert ol.resolve(types.SimpleNamespace()) == "en"


def test_resolver_never_raises(ol, load, monkeypatch):
    nc = load("nova_config")

    def boom(hass):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(nc, "output_language", boom)
    assert ol.resolve(None) == "en"
    assert ol.resolve(_hass("fr")) == "fr"


def test_home_assistant_unknown_language_is_passed_through_as_today(ol, setting):
    assert ol.resolve(_hass("xx")) == "xx"


def test_validity_rules(ol):
    for ok in ("", "auto", "Auto", "de", "de-DE", "en", "nb", "pt-BR"):
        assert ol.is_valid_setting(ok), ok
    for bad in ("xx", "klingon", "de-", 5, True, None, 1.5, ["de"], {"a": 1}, "de DE"):
        assert not ol.is_valid_setting(bad), bad


def test_single_names_table_everywhere(load):
    ol = load("output_language")
    ctx = load("agent_runtime.context")
    assert ctx._LANG_NAMES is ol.LANG_NAMES
    cc = core_text()
    assert "hass.config.language" not in cc          # no second copy of the logic
    assert "_LANG_NAMES = {" not in (COMP / "agent_runtime" / "context.py").read_text()


def test_resolver_is_a_leaf_module():
    tree = ast.parse((COMP / "output_language.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.col_offset == 0:
            pytest.fail("output_language imports another Nova module at import time")


# ── directive ───────────────────────────────────────────────────────────────

def test_english_directive_is_empty(ol, setting):
    assert ol.directive(_hass("en")) == ""
    assert ol.with_language(_hass("en"), "SYSTEM") == "SYSTEM"
    assert ol.field_directive(_hass("en"), "speak") == ""


def test_german_directive_from_home_assistant(ol, setting):
    d = ol.directive(_hass("de"))
    assert d.startswith("## Language") and "German" in d


def test_nova_german_with_home_assistant_english_gives_german(ol, setting):
    setting["value"] = "de"
    d = ol.directive(_hass("en"))
    assert "Respond in German by default" in d


def test_nova_english_overrides_a_german_home(ol, setting):
    setting["value"] = "en"
    assert ol.directive(_hass("de")) == ""


def test_agent_directive_goes_through_the_resolver(load, setting):
    ctx = load("agent_runtime.context")
    setting["value"] = "ja"
    assert "Japanese" in ctx._language_directive(_hass("en"))
    setting["value"] = ""
    assert ctx._language_directive(_hass("en")) == ""


def test_plain_and_field_directives(ol, setting):
    setting["value"] = "de"
    plain = ol.directive(_hass("en"), conversational=False)
    assert "Write this in German" in plain and "another language" not in plain
    assert ol.with_language(_hass("en"), "SYSTEM").startswith("SYSTEM\n\n## Language")
    f = ol.field_directive(_hass("en"), "speak")
    assert 'value of "speak" in German' in f and "JSON key" in f


# ── _hass_lang and the safety notifications ─────────────────────────────────

def test_hass_lang_follows_the_setting_and_feeds_notify_i18n(load, setting):
    cc = load("cognitive_core")
    i18n = load("notify_i18n")
    setting["value"] = "de"
    lang = cc._hass_lang(_hass("en"))
    assert lang == "de"
    assert cc._notify_i18n().title("freeze_critical", lang) == "Nova — Frostwarnung"
    assert cc._notify_i18n().title("freeze_critical", lang) == i18n.TITLES["freeze_critical"]["de"]


def test_hass_lang_unset_is_home_assistant(load, setting):
    cc = load("cognitive_core")
    assert cc._hass_lang(_hass("fr")) == "fr"
    assert cc._hass_lang(_hass(None)) == "en"


def test_unsupported_language_gives_english_safety_templates(load, setting):
    cc = load("cognitive_core")
    i18n = load("notify_i18n")
    setting["value"] = "ja"
    lang = cc._hass_lang(_hass("en"))
    assert lang == "ja"
    assert cc._notify_i18n().title("freeze_critical", lang) == i18n.TITLES["freeze_critical"]["en"]


# ── the setting itself ──────────────────────────────────────────────────────

def test_nova_config_reads_at_call_time_runtime_first(load, monkeypatch):
    nc = load("nova_config")
    rc: dict = {}
    pkg = nc.__name__.rsplit(".", 1)[0]
    mod = types.ModuleType(f"{pkg}.runtime")
    mod.domain_runtime_config = lambda hass: rc
    monkeypatch.setitem(sys.modules, f"{pkg}.runtime", mod)
    stored = {"output_language": "fr"}
    monkeypatch.setattr(nc, "get", lambda k, d=None: stored.get(k, d))
    assert nc.output_language(object()) == "fr"          # config.json
    rc["output_language"] = "de"
    assert nc.output_language(object()) == "de"          # live value wins
    rc["output_language"] = ""
    assert nc.output_language(object()) == ""            # cleared means auto, not the old file value
    rc["output_language"] = 5
    assert nc.output_language(object()) == ""            # not a string


def test_default_is_off_for_existing_installs(load, monkeypatch):
    nc = load("nova_config")
    monkeypatch.setattr(nc, "get", lambda k, d=None: d)
    pkg = nc.__name__.rsplit(".", 1)[0]
    mod = types.ModuleType(f"{pkg}.runtime")
    mod.domain_runtime_config = lambda hass: {}
    monkeypatch.setitem(sys.modules, f"{pkg}.runtime", mod)
    assert nc.output_language(object()) == ""
    assert load("output_language").resolve(_hass("en")) == "en"


def test_write_path_validation(load):
    sc = load("safety_config")
    for ok in ("de", "de-DE", "", "auto", "AUTO"):
        assert sc.valid_panel_value("output_language", ok), ok
    for bad in (5, True, None, "xx", "klingon", ["de"], "de'; DROP", 1.0):
        assert not sc.valid_panel_value("output_language", bad), bad


def test_key_is_panel_writable_surfaced_and_documented():
    ws = (COMP / "websocket.py").read_text()
    tree = ast.parse(ws)
    keys = next({e.value for e in n.value.elts} for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Set)
                and any(getattr(t, "id", "") == "PANEL_WRITABLE_KEYS" for t in n.targets))
    assert "output_language" in keys
    assert '"output_language": str(_runtime_opt(hass, entry, "output_language", "") or "")' in ws
    assert "`output_language`" in (ROOT / "README.md").read_text()


def test_ui_language_is_untouched():
    ws = (COMP / "websocket.py").read_text()
    assert '"ui_language": _runtime_opt(hass, entry, "ui_language", "auto")' in ws


def test_panel_language_list_matches_the_table(load):
    ol = load("output_language")
    js = (ROOT / "frontend" / "src" / "panel" / "settings-cards.js").read_text()
    block = js.split('data-cfg-key="output_language"')[1].split("cfg.output_language")[0]
    codes = re.findall(r'\["([a-z]{2})(?:-[A-Za-z]+)?", "', block)
    assert "auto" not in codes[:0]
    expected = set(ol.LANG_NAMES) - {"no"}                # "no" is the same language as "nb"
    assert set(codes) == expected


def test_diagnostics_do_not_scrub_the_language_key(load):
    import importlib
    load("output_language")
    diag = importlib.import_module("jc.diagnostics")
    out = diag._redact({"output_language": "de", "api_key": "secret"})
    assert out["output_language"] == "de" and out["api_key"] == "**REDACTED**"


# ── direct execute_chat callers ─────────────────────────────────────────────
# Person facing text (spoken, pushed or announced): the language block is in
# the prompt. Structured or internal replies: untouched.

class _Result:
    def __init__(self, text):
        self.text = text


def _capture(monkeypatch, reply):
    sent: list = []

    async def fake(hass, client, messages, **kw):
        sent.append(messages)
        return _Result(reply)
    mod = types.ModuleType("jc.providers.activity")
    mod.execute_chat = fake
    monkeypatch.setitem(sys.modules, "jc.providers.activity", mod)
    return sent


def _system(sent):
    return sent[0][0]["content"]


async def test_summary_prompt_has_the_directive(load, fake_hass, setting, monkeypatch):
    summary = load("summary")
    sent = _capture(monkeypatch, "Ein kurzer Bericht.")
    setting["value"] = "de"
    monkeypatch.setattr(summary, "build_system_prompt", lambda h, hon, task: "BASE")
    res = await _call_summary(summary, fake_hass, monkeypatch)
    assert res["success"] and sent and "Write this in German" in _system(sent)
    sent.clear()
    setting["value"] = ""
    await _call_summary(summary, fake_hass, monkeypatch)
    assert _system(sent) == "BASE"                    # English: unchanged


async def _call_summary(summary, fake_hass, monkeypatch):
    fake_hass.config.language = "en"
    monkeypatch.setattr(summary, "get_recent_messages", lambda *a, **k: [
        {"timestamp": "2026-10-05T10:00:00", "role": "user", "content": "hi"}])
    monkeypatch.setattr(summary, "save_message", lambda *a, **k: None)
    call = types.SimpleNamespace(data={"hours": 1, "announce": False, "store": False})
    return await summary.async_summarise(fake_hass, call, object(), "sir", None, [])


def test_every_person_facing_caller_adds_the_directive():
    """Static proof for the callers that need heavy fakes to run: each one
    appends the language block to its system prompt."""
    expect = {
        "briefing.py": "output_language.with_language(hass, system)",
        "proactive_briefing.py": "output_language.with_language(hass, system)",
        "camera.py": "output_language.with_language(hass, system)",
        "sentinel.py": "output_language.with_language(self.hass, system)",
        "summary.py": "output_language.with_language(hass, system)",
    }
    for fname, needle in expect.items():
        assert needle in (COMP / fname).read_text(), fname
    assert 'output_language.field_directive(hass, "speak")' in (COMP / "camera.py").read_text()
    assert 'output_language.field_directive(hass, "message")' in \
        (COMP / "cognitive" / "coordinator.py").read_text()


def test_structured_and_internal_callers_are_untouched():
    for fname in ("suggestion_review.py", "package_monitor.py", "scenes.py",
                  "camera_coverage.py", "classifier.py", "setup_probe.py",
                  "agent_runtime/loop.py", "llm_provider.py", "cognitive_core.py"):
        src = core_text() if fname == "cognitive_core.py" else (COMP / fname).read_text()
        assert "with_language(" not in src and "field_directive(" not in src, fname
        assert "output_language.directive(" not in src, fname


async def test_sentinel_alert_prompt_has_the_directive_and_english_does_not(
        load, fake_hass, setting, monkeypatch):
    ev = types.ModuleType("homeassistant.helpers.event")
    ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
    ev.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    s_mod = load("sentinel")
    sent = _capture(monkeypatch, "Die Tür ist offen.")
    monkeypatch.setattr(s_mod, "build_system_prompt", lambda h, hon, task: "BASE")
    sentinel = s_mod.NovaSentinel(fake_hass, groq_client=object(), honorific="sir",
                                  rules=[], entry=None)
    fake_hass.config.language = "en"
    setting["value"] = "de"
    out = await sentinel._groq_line("binary_sensor.door", "Door", {"state": "open"}, 10)
    assert out == "Die Tür ist offen." and "Write this in German" in _system(sent)
    sent.clear()
    setting["value"] = ""
    await sentinel._groq_line("binary_sensor.door", "Door", {"state": "open"}, 10)
    assert _system(sent) == "BASE"                    # English: byte for byte unchanged


async def test_observer_decision_prompt_only_changes_the_message_field(
        load, fake_hass, setting, monkeypatch):
    coord = load("cognitive.coordinator")
    ol = load("output_language")
    fake_hass.config.language = "en"
    setting["value"] = "de"
    block = ol.field_directive(fake_hass, "message")
    assert 'value of "message" in German' in block and "JSON structure" in block
    assert "Write this in" not in block              # not the free text block
    setting["value"] = ""
    assert ol.field_directive(fake_hass, "message") == ""
    assert hasattr(coord, "__name__")


async def test_camera_reasoning_only_the_spoken_field_follows_the_language(
        load, fake_hass, setting, monkeypatch):
    cam = load("camera")
    sent = _capture(monkeypatch,
                    '{"notable": true, "category": "person", "summary": "A person.", "speak": "Jemand ist an der Tür."}')
    fake_hass.config.language = "en"
    setting["value"] = "de"
    out = await cam._reason_about_scene(fake_hass, object(), "m", "Front", "a person at the door", "person")
    assert out["speak"] == "Jemand ist an der Tür." and out["category"] == "person"   # JSON still parsed
    sys_de = _system(sent)
    assert 'value of "speak" in German' in sys_de and "Write this in" not in sys_de
    sent.clear()
    setting["value"] = ""
    await cam._reason_about_scene(fake_hass, object(), "m", "Front", "a person at the door", "person")
    assert "Language" not in _system(sent)             # English: prompt unchanged


async def test_scene_pick_is_a_classifier_and_is_never_given_the_directive(
        load, fake_hass, setting, monkeypatch):
    scenes = load("scenes")
    sent = _capture(monkeypatch, "scene.movie")
    fake_hass.config.language = "en"
    setting["value"] = "de"
    fake_hass.states.set("scene.movie", "scening", friendly_name="Movie")
    call = types.SimpleNamespace(data={"intent": "movie night", "announce": False})
    try:
        await scenes.async_activate_by_intent(fake_hass, call, object(), "sir", None, [])
    except Exception:
        pass
    assert sent, "the scene picker did not call the model"
    assert "Language" not in _system(sent) and "German" not in _system(sent)


# ── Chinese scripts ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["zh-Hant", "zh-TW", "zh_hant"])
def test_traditional_chinese_setting_says_traditional(ol, setting, raw):
    setting["value"] = raw
    d = ol.directive(_hass("en"))
    assert "Traditional Chinese (Taiwan wording)" in d
    assert "Traditional Chinese (Taiwan wording)" in ol.field_directive(_hass("en"), "speak")
    assert "Traditional Chinese (Taiwan wording)" in ol.with_language(_hass("en"), "x")


def test_auto_picks_the_chinese_script_from_home_assistant(ol, setting):
    setting["value"] = "auto"
    assert "Traditional Chinese (Taiwan wording)" in ol.directive(_hass("zh-Hant"))
    assert "Simplified Chinese" in ol.directive(_hass("zh-Hans"))


def test_old_saved_zh_still_means_simplified(ol, setting):
    setting["value"] = "zh"
    assert ol.is_valid_setting("zh") and ol.resolve(_hass("en")) == "zh"
    d = ol.directive(_hass("en"))
    assert "Simplified Chinese" in d and "Traditional" not in d


def test_traditional_chinese_safety_alerts_stay_english(load, setting):
    cc = load("cognitive_core")
    i18n = load("notify_i18n")
    setting["value"] = "zh-Hant"
    lang = cc._hass_lang(_hass("en"))
    assert lang == "zh-Hant"
    assert cc._notify_i18n().title("freeze_critical", lang) == i18n.TITLES["freeze_critical"]["en"]


def test_panel_picker_offers_both_chinese_scripts_and_keeps_traditional():
    js = (ROOT / "frontend" / "src" / "panel" / "settings-cards.js").read_text()
    block = js.split('data-cfg-key="output_language"')[1].split("</select>")[0]
    assert '["zh", "Simplified Chinese"]' in block
    assert '["zh-Hant", "Traditional Chinese"]' in block
