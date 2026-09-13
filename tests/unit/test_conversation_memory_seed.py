"""Guards for the memory-seeding fix (11 Sept 2026): a conversation must keep
catching up on cross-session history after real gaps, not just once ever.

These are source-level guards, same reasoning as test_conversation_dispatch.py:
exercising NovaAgent._maybe_seed_history for real needs a live HA conversation
stack, so we assert on the source instead. The actual gap-detection logic
(memory_thread.should_reseed) is a pure function and is unit-tested directly
in test_memory_thread.py.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "conversation.py"


def test_no_permanent_one_shot_seed_flag():
    src = SRC.read_text()
    # The old bug: a conversation_id, once seeded, was marked forever and
    # never caught up again. Guard against that flag coming back.
    assert "_threaded" not in src, \
        "found a permanent 'already seeded' flag — seeding must be gap-based, not one-shot"
    assert "self._last_seen" in src


def test_reseed_decision_delegates_to_should_reseed():
    src = SRC.read_text()
    # The gap check itself must live in memory_thread (pure, unit-tested),
    # not be reimplemented inline in conversation.py.
    assert "memory_thread.should_reseed(" in src


def test_reseed_replaces_window_not_prepends():
    src = SRC.read_text()
    # A reseed must REPLACE the thread's window. Prepending onto it (the old
    # behaviour) would duplicate/grow it without bound once seeding can fire
    # more than once per conversation_id.
    assert "history[:] = [memory_thread.format_seed_message(seeded)]" in src
    assert "history[:0] = seeded" not in src, \
        "reseed is prepending again — will duplicate history across repeated reseeds"


def test_reseed_wraps_seed_in_format_seed_message():
    src = SRC.read_text()
    # Fixed 11 Sept 2026: raw seeded turns spliced straight into history read
    # as LIVE conversation to the model, which made Nova fixate on stale,
    # completed exchanges (e.g. a light turned off days ago). Seeded content
    # must be wrapped as a single system-role note instead — see
    # memory_thread.format_seed_message.
    assert "memory_thread.format_seed_message(" in src


def test_reseed_scoped_to_the_conversation_not_global():
    src = SRC.read_text()
    # Fixed v7.87.0 (backlog #1): reseed used to pull globally across every
    # device/conversation in the house -- one household member's exchange
    # could leak into another's session. Must pass this conversation's own
    # cid through to load_recent, not call it with no scope.
    assert "memory_thread.load_recent(self.hass, hours, limit, device_id=cid)" in src


def test_semantic_memory_context_scoped_to_the_conversation_not_global():
    src = SRC.read_text()
    # Fixed v7.87.0 (backlog #1 follow-up): get_conversation_context() (a
    # SEPARATE semantic-memory subsystem from the reseed above) also used to
    # search globally -- must pass cid through as the third positional arg.
    assert "get_conversation_context, user_input.text, 3, cid," in src
