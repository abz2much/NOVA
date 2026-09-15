"""Voice-triggered "repeat that" Spoken History wiring (v7.104.0, correction
#5's strict option) — source-level guard, same reasoning as
test_conversation_activity_wiring.py's: NovaAgent extends HA's own
conversation.ConversationEntity, so exercising _async_handle_message needs a
live HA conversation stack. spoken_history.py's own behavior is covered in
test_spoken_history.py; local_engine.py's repeat_last branch (which sets
LocalResult.repeat_of_id) is covered in test_local_engine_repeat.py; this
only proves the ONE place a voice-triggered repeat can ever be recorded
(the Cast-paired reply branch, where delivery is confirmed) actually tags
it "repeat" with a reference to the original, and that this is the only
async_announce call site in the whole file that ever passes repeat_of_id —
an ordinary reply that instead plays through the pipeline's own TTS on the
satellite is never touched by any of this.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "conversation.py"


def _function_source(name: str) -> str:
    tree = ast.parse(SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(SRC.read_text(), node)
    raise AssertionError(f"{name} not found in {SRC}")


def test_cast_routed_reply_tags_repeat_when_local_result_carries_repeat_of_id():
    src = _function_source("_handle_message_impl")
    assert 'context="repeat" if repeat_of_id else "reply"' in src
    assert "repeat_of_id=repeat_of_id" in src
    assert "local_result.repeat_of_id" in src


def test_repeat_of_id_is_only_ever_passed_from_the_cast_routed_branch():
    """No other async_announce call site in this file may pass repeat_of_id
    — that would let some other reply path silently create a mislabeled
    'repeat' history entry without going through local_engine's
    deterministic command at all."""
    src = SRC.read_text()
    assert src.count("repeat_of_id=repeat_of_id") == 1
