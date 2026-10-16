"""Apply (nova/apply_ai_config) also checks a cloud role it changes.

Before 8.58.1 only Ollama and custom roles were called before saving, so a
wrong Gemini model or key showed up on the first chat. A cloud role is now
checked when the staged change touches its provider or model; roles left as
they were are not called again."""
from __future__ import annotations

# The fixtures and helpers the other ws_ai tests use.
from test_pin_ws_ai import _apply, _entry, _reloading_hass, ai, cfg, dropped  # noqa: F401


async def test_a_changed_gemini_role_is_checked_before_saving(ai, load, cfg):
    cfg.creds = {"groq", "gemini"}
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass,
                       {"vision_provider": "gemini", "vision_model": "gemini-3.6-flash"})
    assert res["ok"] is True
    await hass.drain()
    assert cfg.tests == [("gemini", "gemini-3.6-flash", "")]
    assert len(cfg.saved) == 1


async def test_a_failed_cloud_check_saves_nothing_and_names_the_role(ai, load, cfg, dropped):
    cfg.creds = {"groq", "gemini"}
    cfg.test_problem = "invalid_auth"
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, {"vision_provider": "gemini", "vision_model": "gemini-3.6-flash"})
    assert res == {"ok": False, "error": "connection_test_failed",
                   "message": "Vision could not use gemini-3.6-flash on gemini."}
    assert cfg.saved == [] and hass.reloads == [] and dropped == []


async def test_changing_only_the_model_checks_that_model(ai, load, cfg):
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, {"model": "a-new-model"})
    assert res["ok"] is True
    await hass.drain()
    assert cfg.tests == [("groq", "a-new-model", "")]


async def test_a_cloud_role_left_as_it_was_is_not_called(ai, load, cfg):
    cfg.creds = {"groq", "gemini"}
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass,
                       {"vision_provider": "gemini", "vision_model": "gemini-3.6-flash"})
    assert res["ok"] is True
    await hass.drain()
    assert [t[0] for t in cfg.tests] == ["gemini"]      # the groq roles were not touched


async def test_one_model_on_two_changed_roles_is_checked_once(ai, load, cfg):
    cfg.creds = {"groq", "gemini"}
    updates = {"vision_provider": "gemini", "vision_model": "gemini-3.6-flash",
               "camera_reasoning_provider": "gemini", "camera_reasoning_model": "gemini-3.6-flash"}
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, updates)
    assert res["ok"] is True
    await hass.drain()
    assert cfg.tests == [("gemini", "gemini-3.6-flash", "")]
