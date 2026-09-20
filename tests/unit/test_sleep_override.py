"""Tests for the explicit sleep-state override (v7.86.0): Auto/Awake/Asleep,
set directly or via the nightly "Heading to bed?" prompt. This exists because
sleep_detection.is_sleeping() is house-wide occupancy — one person going to
bed otherwise marks the whole house "asleep" even while someone else is up.
The override lets a deliberate answer beat that inference, and expires on its
own at the next quiet-hours end so it can't linger into the following day.
"""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def jcfg(load, tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    # A couple of other test files (test_service_health.py,
    # test_recognition_frigate.py) swap sys.modules["jc.nova_config"] for a
    # throwaway fake and never restore it, so a cached entry left behind by an
    # earlier-collected test could be that fake rather than the real module.
    # Force a genuine reload of the real module for this fixture regardless
    # of collection order.
    sys.modules.pop("jc.nova_config", None)
    j = load("nova_config")
    monkeypatch.setattr(j, "CONFIG_PATH", Path(tmp_path / "config.json"))
    monkeypatch.setattr(j, "_cache", {})
    monkeypatch.setattr(j, "_loaded", False)
    return j


@pytest.fixture
def sd(load, jcfg):
    import sys
    # Force a fresh reload too: if some earlier-collected test already
    # triggered cognitive_core's lazy `from . import sleep_detection`, that
    # cached module's own `nova_config` name is bound to whatever instance
    # was live back then — not the freshly-reloaded, tmp_path-scoped one
    # `jcfg` just set up. Reloading here re-binds it correctly.
    sys.modules.pop("jc.sleep_detection", None)
    return load("sleep_detection")


def _freeze(monkeypatch, sd, now: datetime):
    """Pin dt_util.now()/utcnow() to the same aware instant, so override
    expiry math is deterministic regardless of the real wall clock."""
    monkeypatch.setattr(sd.dt_util, "now", lambda tz=None: now)
    monkeypatch.setattr(sd.dt_util, "utcnow", lambda: now)


def test_no_override_by_default(sd, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc))
    assert sd._read_override() == ("auto", None)


def test_set_asleep_forces_sleeping_despite_no_occupancy(sd, fake_hass, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 15, tzinfo=timezone.utc))
    sd.set_override("asleep", "07:00")
    sleeping, reason = sd.is_sleeping(fake_hass, bedroom_area_ids=[], quiet_start="22:00", quiet_end="07:00")
    assert sleeping is True
    assert reason == "set to Asleep"


def test_set_awake_beats_bedroom_occupancy(sd, fake_hass, monkeypatch):
    """The exact false-positive this override exists for: someone is in a
    bedroom-flagged area during quiet hours, but a resident has explicitly
    said Awake — the house must NOT read as asleep."""
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 15, tzinfo=timezone.utc))
    fake_hass.states.set("binary_sensor.bedroom_occupancy", "on",
                          device_class="occupancy")
    monkeypatch.setattr(sd, "is_any_bedroom_occupied",
                         lambda hass, areas: (True, "bedroom"))
    sd.set_override("awake", "07:00")
    sleeping, reason = sd.is_sleeping(
        fake_hass, bedroom_area_ids=["bedroom"], quiet_start="22:00", quiet_end="07:00")
    assert sleeping is False
    assert reason == "set to Awake"


def test_override_expires_at_quiet_end_and_falls_back_to_auto(sd, fake_hass, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 15, tzinfo=timezone.utc))
    sd.set_override("asleep", "07:00")
    assert sd._read_override()[0] == "asleep"

    # Past the stored expiry (next day's 07:00) — override must be spent.
    _freeze(monkeypatch, sd, datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc))
    assert sd._read_override() == ("auto", None)
    sleeping, reason = sd.is_sleeping(
        fake_hass, bedroom_area_ids=[], quiet_start="22:00", quiet_end="07:00")
    assert sleeping is False
    assert reason == "awake"


def test_current_override_reflects_expiry(sd, monkeypatch):
    """The panel dropdown reads current_override(), not the raw stored key —
    it must flip back to 'auto' the moment expiry passes, not stay stuck on
    'asleep' forever (the bug this function was added to fix)."""
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 15, tzinfo=timezone.utc))
    sd.set_override("asleep", "07:00")
    assert sd.current_override() == "asleep"

    _freeze(monkeypatch, sd, datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc))
    assert sd.current_override() == "auto"


def test_set_auto_clears_override_immediately(sd, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 15, tzinfo=timezone.utc))
    sd.set_override("asleep", "07:00")
    sd.set_override("auto")
    assert sd._read_override() == ("auto", None)


def test_invalid_override_value_rejected(sd):
    with pytest.raises(ValueError):
        sd.set_override("napping")


# ── nightly prompt eligibility ───────────────────────────────────────────────

def test_prompt_not_eligible_before_prompt_time(sd, fake_hass, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 22, 30, tzinfo=timezone.utc))
    assert sd._prompt_eligible(fake_hass, {"sleep_prompt_time": "23:00"}) is False


def test_prompt_not_eligible_when_tv_on(sd, fake_hass, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc))
    fake_hass.states.set("media_player.lounge_tv", "playing")
    cfg = {"sleep_prompt_time": "23:00", "movie_media_player": "media_player.lounge_tv"}
    assert sd._prompt_eligible(fake_hass, cfg) is False


def test_prompt_eligible_past_time_with_tv_off(sd, fake_hass, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc))
    fake_hass.states.set("media_player.lounge_tv", "off")
    cfg = {"sleep_prompt_time": "23:00", "movie_media_player": "media_player.lounge_tv"}
    assert sd._prompt_eligible(fake_hass, cfg) is True


def test_prompt_not_eligible_when_already_overridden(sd, fake_hass, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc))
    sd.set_override("awake", "07:00")
    assert sd._prompt_eligible(fake_hass, {"sleep_prompt_time": "23:00"}) is False


def test_prompt_not_eligible_when_disabled(sd, fake_hass, monkeypatch):
    _freeze(monkeypatch, sd, datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc))
    cfg = {"sleep_prompt_time": "23:00", "sleep_prompt_enabled": False}
    assert sd._prompt_eligible(fake_hass, cfg) is False


def test_maybe_prompt_marks_today_so_it_only_fires_once(sd, fake_hass, monkeypatch):
    now = datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc)
    _freeze(monkeypatch, sd, now)
    monkeypatch.setattr(sd, "_PROMPTED_DATE", None)
    sent = []

    async def _fake_send(hass, quiet_end, config):
        sent.append((quiet_end, config))

    monkeypatch.setattr(sd, "_send_sleep_prompt", _fake_send)

    import asyncio
    cfg = {"sleep_prompt_time": "23:00", "observer_quiet_end": "07:00"}
    asyncio.run(sd.maybe_prompt_sleep(fake_hass, cfg))
    assert sent == [("07:00", cfg)]
    assert sd._PROMPTED_DATE == now.date().isoformat()

    # Same tick again later tonight — must not re-send.
    asyncio.run(sd.maybe_prompt_sleep(fake_hass, cfg))
    assert sent == [("07:00", cfg)]
