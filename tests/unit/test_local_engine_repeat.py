"""Deterministic local "repeat that" command (v7.104.0).

local_engine.try_local is PRIMARY and runs before any LLM call (see
conversation.py:941 — try_local is always tried first, unconditionally).
This command has zero code paths that reference an agent/LLM client at
all — it's a pure in-memory read plus a fixed string — so proving it
returns a handled LocalResult for each trigger phrase is sufficient proof
no model is ever consulted; there's nothing here that could call one.

spoken_history.py's own functions are tested directly in
test_spoken_history.py; this file only covers local_engine.py's use of
them (the pattern match, the fixed no-history response, and repeat_of_id
threading onto LocalResult).
"""
from __future__ import annotations

import pytest

from fakes import FakeHass


@pytest.fixture
def le(load):
    return load("local_engine")


@pytest.fixture
def sh(load):
    mod = load("spoken_history")
    mod._last = None
    return mod


@pytest.mark.parametrize("phrase", [
    "repeat that",
    "say that again",
    "what did you just say",
    "repeat your last announcement",
    "Repeat That",  # case-insensitive
])
async def test_repeat_phrases_return_the_last_spoken_text_without_an_llm_call(le, sh, phrase):
    # local_engine reads only the in-memory mirror (get_last()), never
    # SQLite directly (see spoken_history.py's module docstring) — set it
    # directly here, the same state a real record() leaves behind on
    # success, without needing a writable db path in this test.
    sh._last = {"id": 7, "text": "Welcome home, sir.", "source": "welcome",
                "speakers": ["media_player.kitchen"], "repeat_of_id": None}
    result = await le.try_local(FakeHass(), phrase, "sir")
    assert result is not None
    assert result.handled is True  # never falls through to the LLM
    assert result.text == "Welcome home, sir."
    assert result.success is True


async def test_repeat_sets_repeat_of_id_to_the_last_entrys_id(le, sh):
    sh._last = {"id": 42, "text": "Reminder: bins.", "source": "reminder",
                "speakers": ["media_player.a"], "repeat_of_id": None}
    result = await le.try_local(FakeHass(), "repeat that", "sir")
    assert result.repeat_of_id == 42


async def test_no_history_returns_the_exact_required_message_and_is_still_handled(le, sh):
    result = await le.try_local(FakeHass(), "repeat that", "sir")
    assert result is not None
    assert result.handled is True  # still never escalates to the LLM
    assert result.success is False
    assert result.text == "I don't have a recent spoken message to repeat."


async def test_repeat_works_after_a_simulated_restart_via_hydrate(le, sh, tmp_path):
    """The exact scenario correction #3 requires: after a restart, 'repeat
    that' must recall the latest retained message without Nova needing to
    speak something new first — i.e. hydrate() alone is enough, no fresh
    record() call in this process."""
    db = str(tmp_path / "conversations.db")
    sh.record("last message before restart", "briefing", ["media_player.a"], db_path=db)
    sh._last = None  # simulate the mirror being empty after a fresh process start

    result_before_hydrate = await le.try_local(FakeHass(), "repeat that", "sir")
    assert result_before_hydrate.text == "I don't have a recent spoken message to repeat."

    sh.hydrate(db_path=db)  # the async_setup_entry startup step
    result_after_hydrate = await le.try_local(FakeHass(), "repeat that", "sir")
    assert result_after_hydrate.success is True
    assert result_after_hydrate.text == "last message before restart"


async def test_repeat_does_not_match_unrelated_phrases(le, sh):
    sh._last = {"id": 1, "text": "Welcome home, sir.", "source": "welcome",
                "speakers": ["media_player.kitchen"], "repeat_of_id": None}
    result = await le.try_local(FakeHass(), "turn on the kitchen light", "sir")
    # Whatever this resolves to (or None, falling through to the agent), it
    # must never be the repeat text — the pattern must not over-match.
    assert result is None or result.text != "Welcome home, sir."
