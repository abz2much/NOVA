"""The arrival briefing used to tell the LLM two things that collided:
"Begin with 'Good afternoon, sir.'" and, separately, "Alex just arrived
home" — producing redundant lines like "Good afternoon, sir. Alex has just
arrived home." when the person being addressed (via their own honorific,
see honorific.py) IS Alex. It also always included open doors/windows in
the prompt context, even though the arrival trigger IS a door opening —
stating the obvious ("the front door is open") on the very briefing that
door-open caused.

These tests call the real `_trigger_briefing("arrival", ...)` and inspect
what actually reached the LLM (captured via FakeProvider.last_messages),
stubbing only the network/IO seams (LLM provider, TTS, phone push) that
aren't part of the prompt-building logic under test.
"""
import sys
import types
from datetime import datetime

import pytest


@pytest.fixture
def pb(load, monkeypatch, fake_hass):
    honorific_mod = load("honorific")
    mod = load("proactive_briefing")
    mod._STATE.hass = fake_hass
    mod._STATE.config = {}
    mod._STATE.running = True
    mod._STATE.last_briefing_time = 0.0

    # Force the honorific to belong to the one person who's home (Alex) —
    # simulates honorific.effective_honorific() resolving to his own
    # configured honorific because he's home alone.
    monkeypatch.setattr(honorific_mod, "effective_honorific", lambda hass: "sir")

    # conftest globally stubs build_system_prompt to a fixed string (it's a
    # shared stub used by unrelated reasoning_loop tests); here we need to
    # see the actual `task` text this module builds, so echo it back instead.
    monkeypatch.setattr(
        sys.modules["jc.directive_helper"], "build_system_prompt",
        lambda hass, honorific, task: task,
    )

    # Network/IO seams unrelated to the prompt-building logic under test.
    fake_provider = types.SimpleNamespace(
        chat=lambda messages, *a, **k: ({"text": "stubbed reply"}, messages)[0]
    )
    captured = {}

    def _fake_create_tier_provider(config, tier):
        return fake_provider

    llm_stub = types.ModuleType("jc.llm_provider")
    llm_stub.create_provider = lambda *a, **k: fake_provider
    llm_stub.create_tier_provider = _fake_create_tier_provider
    monkeypatch.setitem(sys.modules, "jc.llm_provider", llm_stub)

    def _capture_chat(messages, *a, **k):
        captured["messages"] = messages
        return {"text": "stubbed reply"}
    fake_provider.chat = _capture_chat

    mod._captured = captured
    return mod


def test_arrival_briefing_does_not_restate_name_when_addressing_directly(pb, fake_hass):
    import asyncio
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    system_msg = pb._captured["messages"][0]["content"]
    user_msg = pb._captured["messages"][1]["content"]
    assert "Welcome home, sir." in system_msg
    assert "Alex just arrived home" not in system_msg
    assert "Alex just arrived home" not in user_msg


def test_arrival_briefing_drops_open_door_from_context(pb, fake_hass):
    import asyncio
    fake_hass.states.set("binary_sensor.front_door", "on",
                          friendly_name="Front Door Contact Sensor", device_class="door")
    fake_hass.states.set("lock.garage", "unlocked", friendly_name="Garage")
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    user_msg = pb._captured["messages"][1]["content"]
    assert "Front Door Contact Sensor is open" not in user_msg
    assert "Garage is unlocked" in user_msg


def test_arrival_welcomes_the_arriver_by_their_honorific_with_others_home(pb, fake_hass, monkeypatch):
    """Someone else already home used to drop the honorific and the welcome,
    so the briefing opened with a bare 'Good afternoon.'"""
    import asyncio
    honorific_mod = sys.modules[pb.__name__.rsplit(".", 1)[0] + ".honorific"]
    monkeypatch.setattr(honorific_mod, "effective_honorific", lambda hass: "")
    monkeypatch.setattr(honorific_mod, "arrival_honorific",
                        lambda hass, eid: "ma'am" if eid == "person.morgan" else "")
    asyncio.run(pb._trigger_briefing("arrival", person_name="Morgan",
                                     person_entity="person.morgan"))
    assert "Begin with 'Welcome home, ma'am.'" in pb._captured["messages"][0]["content"]


def test_arrival_without_honorific_still_welcomes_home(pb, fake_hass, monkeypatch):
    import asyncio
    honorific_mod = sys.modules[pb.__name__.rsplit(".", 1)[0] + ".honorific"]
    monkeypatch.setattr(honorific_mod, "arrival_honorific", lambda hass, eid: "")
    asyncio.run(pb._trigger_briefing("arrival", person_name="Morgan",
                                     person_entity="person.morgan"))
    system_msg = pb._captured["messages"][0]["content"]
    assert "Begin with 'Welcome home.'" in system_msg
    assert "Good afternoon" not in system_msg and "Good morning" not in system_msg


def test_proactive_briefing_keeps_its_exact_us_date_and_time(pb, fake_hass, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 6, 16, 5)
            return value.replace(tzinfo=tz) if tz else value

    fake_hass.config.country = "US"
    monkeypatch.setattr(pb, "datetime", FixedDateTime)
    import asyncio
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    user_msg = pb._captured["messages"][1]["content"]
    assert "It is Tuesday October 6, 4:05 PM." in user_msg


def test_arrival_leaves_out_camera_detections_from_hours_ago(pb, fake_hass):
    import asyncio
    import time as _time
    pb._SNAPSHOTS.clear()
    pb.record_snapshot("Doorbell", "camera.doorbell",
                       "A delivery driver at the front door with a parcel.", "doorbell")
    pb._SNAPSHOTS[-1].timestamp = _time.time() - 3 * 3600
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    assert "delivery driver" not in pb._captured["messages"][1]["content"]

    pb._STATE.last_briefing_time = 0.0
    pb.record_snapshot("Doorbell", "camera.doorbell",
                       "A cat on the doorstep.", "motion")
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    user_msg = pb._captured["messages"][1]["content"]
    assert "Latest, at " in user_msg and "A cat on the doorstep." in user_msg
    assert "past tense" in pb._captured["messages"][0]["content"]
    pb._SNAPSHOTS.clear()
