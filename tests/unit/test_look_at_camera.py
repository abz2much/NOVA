"""The look_at_camera vision tool (v6.58.0).

The real snapshot and vision call need Home Assistant and a vision provider,
so the tool is tested in its two halves:

* _shape_look_at_camera_result turns a vision result into the tool's JSON.
  It is pure, so its success and failure shapes are pinned directly.
* _exec_look_at_camera checks its arguments and then calls the module's own
  _analyze_camera, which exists so a test can replace that one name in the
  tool's globals. Nothing deeper in the camera stack is swapped out (doing
  that once failed only in CI on one CPython build).
"""
import json
import sys
import types

import pytest


@pytest.fixture
def agent(load):
    return load("agent")


def _shape(agent, result, entity_id="camera.x"):
    return json.loads(agent._shape_look_at_camera_result(result, entity_id))


@pytest.fixture
def vision(agent, monkeypatch):
    """Replaces the vision call. Set .reply (a dict) or .error (an exception);
    every call's arguments land in .calls."""
    stub = types.SimpleNamespace(reply={"success": True, "analysis": "clear",
                                        "camera": "Shop", "source": "nest"},
                                 error=None, calls=[])

    async def fake(hass, entity_id, prompt, announce, honorific):
        stub.calls.append(dict(entity_id=entity_id, prompt=prompt,
                               announce=announce, honorific=honorific))
        if stub.error is not None:
            raise stub.error
        return stub.reply

    monkeypatch.setitem(agent._exec_look_at_camera.__globals__, "_analyze_camera", fake)
    monkeypatch.setitem(sys.modules, "jc.honorific", types.SimpleNamespace(
        effective_honorific=lambda hass: "ma'am"))
    return stub


async def _ask(agent, hass, **args):
    return json.loads(await agent._exec_look_at_camera(hass, args))


# ── argument checks ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {},
    {"entity_id": "camera.x"},                       # no question
    {"question": "anything?"},                       # no camera
    {"entity_id": "   ", "question": "anything?"},   # blank camera
    {"entity_id": "camera.x", "question": "  "},     # blank question
])
async def test_missing_arguments_are_refused(agent, fake_hass, vision, args):
    out = await _ask(agent, fake_hass, **args)
    assert out == {"error": "entity_id and question are required"}
    assert vision.calls == []                         # never reached the camera


# ── the call through to vision ──────────────────────────────────────────────

async def test_question_reaches_vision_and_answer_comes_back(agent, fake_hass, vision):
    vision.reply = {"success": True, "analysis": "Yes, a red mug.",
                    "camera": "Kitchen", "source": "frigate"}
    out = await _ask(agent, fake_hass, entity_id=" camera.kitchen ",
                     question=" Is there a mug on the counter? ")
    assert out == {"success": True, "camera": "Kitchen",
                   "answer": "Yes, a red mug.", "source": "frigate"}
    (call,) = vision.calls
    assert call["entity_id"] == "camera.kitchen"
    assert "Is there a mug on the counter?" in call["prompt"]
    assert call["announce"] is False                  # quiet unless asked
    assert call["honorific"] == "ma'am"


async def test_announce_flag_is_passed_on(agent, fake_hass, vision):
    await _ask(agent, fake_hass, entity_id="camera.x", question="q?", announce=1)
    assert vision.calls[0]["announce"] is True


async def test_vision_failure_is_shaped_with_a_hint(agent, fake_hass, vision):
    vision.reply = {"success": False, "error": "no_image"}
    out = await _ask(agent, fake_hass, entity_id="camera.porch", question="q?")
    assert out["success"] is False
    assert out["camera"] == "camera.porch"
    assert out["error"] == "no_image"
    assert out["hint"]


async def test_a_crash_in_vision_becomes_an_error_reply(agent, fake_hass, vision):
    vision.error = RuntimeError("provider exploded")
    out = await _ask(agent, fake_hass, entity_id="camera.x", question="q?")
    assert set(out) == {"error"}
    assert out["error"]


# ── result shaping ──────────────────────────────────────────────────────────

def test_success_shape(agent):
    out = _shape(agent, {"success": True,
                         "analysis": "Yes — a soldering iron is on the bench.",
                         "camera": "Workshop", "source": "frigate"}, "camera.workshop")
    assert out["success"] is True
    assert "soldering iron" in out["answer"]
    assert out["camera"] == "Workshop"
    assert out["source"] == "frigate"
    assert "hint" not in out and "error" not in out


def test_success_without_hint_has_exactly_four_keys(agent):
    out = _shape(agent, {"success": True, "analysis": "clear", "camera": "Shop",
                         "source": "nest"}, "camera.shop")
    assert out == {"success": True, "camera": "Shop", "answer": "clear", "source": "nest"}


def test_failure_keeps_the_camera_and_error(agent):
    out = _shape(agent, {"success": False, "error": "no_image", "camera": "Backyard"},
                 "camera.backyard")
    assert (out["success"], out["camera"], out["error"]) == (False, "Backyard", "no_image")
    assert "vision provider" in out["hint"]          # says what to check


def test_failure_defaults(agent):
    out = _shape(agent, {"success": False}, "camera.x")
    assert out["success"] is False
    assert out["camera"] == "camera.x"               # the entity id stands in
    assert out["error"] == "vision analysis failed"
    assert "answer" not in out


def test_empty_result_counts_as_failure(agent):
    assert _shape(agent, {}, "camera.y")["success"] is False
