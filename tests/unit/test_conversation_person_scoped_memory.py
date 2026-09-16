"""Phase 2 — source-level guards for conversation.py's person-scoped memory
wiring, kept deliberately small: behavioral proof (continuity, isolation,
self-retrieval prevention) lives in
tests/integration/test_person_scoped_memory_continuity.py (PHACC, real
conversation stack). These few checks cover code-shape facts that are hard
to observe behaviorally — same reasoning as
test_conversation_relevance_gate_persistence.py, which this file sits
alongside."""
from pathlib import Path

SRC_PATH = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "conversation.py"
SRC = SRC_PATH.read_text()


def _index(marker: str) -> int:
    i = SRC.find(marker)
    assert i != -1, f"marker not found in conversation.py: {marker!r}"
    return i


def test_identity_resolved_exactly_once_per_turn():
    # One ACTUAL call site (an assignment) in the whole per-turn
    # persistence/knowledge/log-command path -- knowledge injection and
    # command logging must reuse the captured `ident`, not call
    # identity_module.resolve() again. Counts only real code, not the
    # several comments in this file that legitimately mention
    # "identity.resolve()" in prose while explaining this exact guarantee.
    import re
    identity_resolve_assignments = re.findall(
        r"^\s*ident\s*=\s*identity(?:_module)?\.resolve\(", SRC, re.MULTILINE,
    )
    assert len(identity_resolve_assignments) == 1, (
        f"expected exactly one `ident = identity(_module).resolve(...)` "
        f"call site in conversation.py, found {len(identity_resolve_assignments)}"
    )


def test_retrieval_precedes_storage_for_the_user_turn():
    # The self-retrieval fix: get_conversation_context (search) must occur
    # before store_memory (write) for the user's own message.
    retrieve_idx = _index("from .memory import get_conversation_context")
    store_idx = _index("from .memory import store_memory")
    assert retrieve_idx < store_idx


def test_retrieval_and_storage_are_separate_fail_open_blocks():
    # Two independent try/except pairs, not one shared block -- a retrieval
    # failure must not prevent storage, and vice versa.
    retrieve_try_idx = _index("from .memory import get_conversation_context")
    store_try_idx = _index("from .memory import store_memory")
    between = SRC[retrieve_try_idx:store_try_idx]
    assert "except Exception as exc:" in between
    assert '_LOGGER.debug("Memory retrieve: %s", exc)' in between
    # The storage import/call must be OUTSIDE the retrieval's try block --
    # i.e. the retrieval's except has already closed before store's own
    # `try:` begins. Confirmed by the persona-injection line sitting
    # between them, unindented to function-body level.
    assert 'persona = persona + "\\n\\n" + mem_context' in between


def test_turn_id_only_created_from_a_real_row_id():
    assert 'turn_id = str(user_row_id) if user_row_id is not None else None' in SRC


def test_user_row_id_captured_via_executor_before_turn_id():
    row_id_idx = _index("user_row_id = await self.hass.async_add_executor_job(\n            save_message,")
    turn_id_idx = _index('turn_id = str(user_row_id) if user_row_id is not None else None')
    assert row_id_idx < turn_id_idx


def test_assistant_semantic_write_threads_the_same_turn_id_and_subject():
    assistant_store_idx = SRC.rindex(
        'lambda: store_memory(response_text, role="assistant",')
    block = SRC[assistant_store_idx:assistant_store_idx + 300]
    assert "subject=episodic_subject" in block
    assert "turn_id=turn_id" in block


def test_knowledge_injection_reuses_captured_identity_no_reresolve():
    kn_idx = _index("if ident is not None:")
    block = SRC[kn_idx:kn_idx + 500]
    assert "identity_module.subject_for(ident)" in block
    assert ".resolve(" not in block


def test_command_log_reuses_captured_identity_no_reresolve():
    who_idx = _index("if ident is not None:\n                    who = ident.person")
    block = SRC[who_idx:who_idx + 300]
    assert "identity_module.UNKNOWN" in block
    assert ".resolve(" not in block
