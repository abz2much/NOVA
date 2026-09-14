"""Regression test for a real bug caught live: the doorbell/camera task
prompts used to say "Focus on what {honorific} would want to know" — the
model echoed that framing back as a third-person subject ("Sir has a visitor
at the door, sir") instead of only using the honorific as a trailing vocative
("You have a visitor, sir"). Fix: the task instruction never mentions the
honorific at all — build_system_prompt's persona rules already own correct
addressing. This just asserts the honorific string never reappears in the
task prompt text, so the pattern can't silently come back."""
import pytest


@pytest.fixture
def cam(load):
    return load("camera")


async def test_doorbell_press_prompt_never_echoes_honorific(cam, fake_hass, monkeypatch):
    captured = {}

    async def _fake_analyze(hass, call, *a, **k):
        captured["prompt"] = call.data.get("prompt", "")
        return {"success": False}  # short-circuits before the event-media fallback
    monkeypatch.setattr(cam, "async_analyze_camera", _fake_analyze)

    async def _no_event_image(*a, **k):
        return None
    monkeypatch.setattr(cam, "_fetch_event_media_image", _no_event_image)

    await cam._analyze_doorbell_press(
        fake_hass, None, "sir", "tts.x", ["media_player.y"],
        "camera.front_door_bell", "Someone rang the bell",
    )
    assert "sir" not in captured["prompt"].lower()
    assert "resident" in captured["prompt"].lower()


async def test_motion_analysis_prompt_never_echoes_honorific(cam, fake_hass, monkeypatch):
    captured = {}

    async def _fake_analyze(hass, call, *a, **k):
        captured["prompt"] = call.data.get("prompt", "")
        return {"success": False}
    monkeypatch.setattr(cam, "async_analyze_camera", _fake_analyze)

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(cam.asyncio, "sleep", _no_sleep)

    await cam.async_auto_analyze_on_event(
        fake_hass, None, "ma'am", "tts.x", ["media_player.y"],
        "camera.backyard", reason="Motion detected", doorbell=False,
    )
    assert "ma'am" not in captured["prompt"].lower()
    assert "resident" in captured["prompt"].lower()
