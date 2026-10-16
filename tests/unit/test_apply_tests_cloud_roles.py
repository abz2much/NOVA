"""Apply (nova/apply_ai_config) also checks a cloud role it changes.

Before 8.58.1 only Ollama and custom roles were called before saving, so a
wrong Gemini model or key showed up on the first chat. A cloud role is now
checked when this Apply really changes its provider or model (the panel sends
every role each time, so the value is compared with the saved one). A failed
cloud check saves anyway and warns; a failed self-hosted check still stops the
save."""
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


async def test_a_failed_cloud_check_still_saves_and_warns_naming_the_role(ai, load, cfg, dropped):
    cfg.creds = {"groq", "gemini"}
    cfg.test_problem = "invalid_auth"
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, {"vision_provider": "gemini", "vision_model": "gemini-3.6-flash"})
    assert res["ok"] is True
    assert res["warnings"] == ["Vision could not use gemini-3.6-flash on gemini."]
    assert "Vision could not use gemini-3.6-flash on gemini." in res["message"]
    assert "saved" in res["message"]
    await hass.drain()
    assert len(cfg.saved) == 1 and len(hass.reloads) == 1


async def test_a_failed_self_hosted_check_still_saves_nothing(ai, load, cfg, dropped):
    cfg.creds = {"groq"}
    cfg.test_problem = "cannot_connect"
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, {"vision_provider": "ollama", "vision_model": "llava",
                                  "ollama_base_url": "http://10.0.0.5:11434"})
    assert res["ok"] is False and res["error"] == "connection_test_failed"
    assert cfg.saved == [] and hass.reloads == []


async def test_a_clean_apply_has_no_warnings(ai, load, cfg):
    cfg.creds = {"groq", "gemini"}
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, {"vision_provider": "gemini", "vision_model": "gemini-3.6-flash"})
    assert res["ok"] is True and "warnings" not in res
    assert res["message"] == "AI settings saved. Nova is reloading."


async def test_the_whole_panel_form_only_checks_the_role_that_changed(ai, load, cfg):
    """The panel sends every role on every Apply."""
    cfg.creds = {"groq", "gemini"}
    updates = dict(cfg.config)                       # every role, as saved
    updates["reasoning_provider"] = "gemini"
    updates["reasoning_model"] = "gemini-3.6-flash"
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, updates)
    assert res["ok"] is True
    await hass.drain()
    assert cfg.tests == [("gemini", "gemini-3.6-flash", "")]


async def test_applying_a_form_with_nothing_changed_calls_no_cloud_model(ai, load, cfg):
    hass = _reloading_hass(_entry(load))
    res = await _apply(ai, hass, dict(cfg.config))
    assert res["ok"] is True
    await hass.drain()
    assert cfg.tests == []


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
