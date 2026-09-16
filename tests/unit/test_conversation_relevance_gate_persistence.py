"""Phase 1 guards: the relevance decision must be made, and enforced, BEFORE
any state mutation relevant to reasoning memory — _maybe_seed_history()
(which updates _last_seen[cid] and may replace the in-memory history),
in-session history mutation, the conversation DB write, semantic-memory
writes/recall, and curated-knowledge injection — but a reply to a pending
offer must stay relevant (and therefore still get persisted) even when it
doesn't parse as a plain "yes"/"no" and even when it would otherwise fail
the addressed-to-Nova check. get_pending_offer() must be read exactly once
and that same object reused by the offer handler, not re-read.

Same reasoning as test_conversation_dispatch.py and
test_conversation_memory_seed.py: exercising NovaAgent._handle_message_impl
for real needs a live HA conversation stack (ConversationEntity, IntentResponse,
the HA Assist LLM API, ...), so these are source-level guards, plus a
formula check that EXECUTES the real `relevant = ...` expression extracted
from the source (not a hand-copied duplicate of it), so a typo or logic
change in conversation.py cannot silently drift out of sync with this test.

A companion PHACC integration test
(tests/integration/test_relevance_gate_persistence.py) drives real turns
through the real conversation stack for the same seven scenarios, for
environments where pytest-homeassistant-custom-component is installed.
"""
import ast
from pathlib import Path

SRC_PATH = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "conversation.py"
SRC = SRC_PATH.read_text()


def _index(marker: str) -> int:
    i = SRC.find(marker)
    assert i != -1, f"marker not found in conversation.py: {marker!r}"
    return i


def _extract_relevant_expr() -> str:
    """Pull the RHS of the real `relevant = ...` assignment out of the actual
    source via ast, so the truth-table test below executes the real formula
    instead of a hand-copied stand-in that could silently drift from it."""
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "relevant"):
            return ast.get_source_segment(SRC, node.value)
    raise AssertionError("no `relevant = ...` assignment found in conversation.py")


# ── 1. Ordering: the five persistence-relevant writes must come after the
#    new relevance decision and its early return ─────────────────────────────

def test_relevance_decision_precedes_all_five_persistence_writes():
    gate_idx = _index("if not relevant:")
    writes = {
        "in-session history append": 'history.append({"role": "user", "content": user_input.text})',
        "conversation DB write": 'save_message, "user", user_input.text, cid, episodic_subject)',
        "semantic-memory write": "lambda: store_memory(user_input.text, role=\"user\",",
        "semantic recall": "get_conversation_context, user_input.text, 3, cid,",
        "curated-knowledge injection": "lambda: knowledge.prompt_block(user_input.text, subjects=subjects))",
    }
    for label, marker in writes.items():
        assert _index(marker) > gate_idx, (
            f"{label} ({marker!r}) must occur AFTER the relevance decision's "
            f"early return, not before it"
        )


def test_relevance_decision_precedes_history_accessor_and_seed():
    # _maybe_seed_history() updates _last_seen[cid] and may replace the
    # in-memory history; _history() creates/returns self._histories[cid].
    # Both must be unreachable for rejected speech, not just the five
    # persistence writes that follow them.
    gate_idx = _index("if not relevant:")
    assert _index("history   = self._history(cid)") > gate_idx
    assert _index("await self._maybe_seed_history(cid, history, subject=episodic_subject)") > gate_idx


def test_persona_construction_is_the_only_thing_left_before_the_decision():
    # Persona construction is the one thing allowed to precede the decision
    # (it only refreshes a static, file-derived cache and reads live HA
    # state — no reasoning-memory mutation) — but everything else between
    # `cid = ...` and the decision must be gone. Only cid/honorific/persona
    # setup may sit between them.
    cid_idx = _index('cid       = user_input.conversation_id or user_input.device_id or "default"')
    gate_idx = _index("if not relevant:")
    between = SRC[cid_idx:gate_idx]
    assert "persona   = self._persona()" in between
    # Check actual CODE, not this test's own explanatory comment text (the
    # comment legitimately mentions _maybe_seed_history by name).
    assert "await self._maybe_seed_history(cid, history)" not in between
    assert "history   = self._history(cid)" not in between
    assert "history.append(" not in between
    assert "save_message(" not in between


def test_relevance_early_return_body_is_silent():
    # The rejected-speech branch must still return empty speech, not route
    # anywhere and not raise.
    block_start = _index("if not relevant:")
    block = SRC[block_start:block_start + 500]
    assert 'ir.async_set_speech("")' in block
    assert "return conversation.ConversationResult(response=ir, conversation_id=cid)" in block


def test_no_local_engine_or_agent_call_before_relevance_decision():
    # Requirement 7: no LLM/provider/HA-action path is even reachable for
    # rejected speech — try_local/run_agent must appear strictly after the
    # new early return, exactly as they did after the old (later) gate.
    gate_idx = _index("if not relevant:")
    assert _index("from .local_engine import try_local, score_complexity") > gate_idx
    assert _index("from .agent import run_agent") > gate_idx


# ── 2. The formula itself: pending offer OR gate disabled OR addressed ───────

def test_relevant_formula_present_verbatim():
    assert "relevant = bool(pending_offer) or not gate_enabled or is_addressed" in SRC


def test_relevant_truth_table_matches_the_real_source_expression():
    expr_src = _extract_relevant_expr()
    code = compile(expr_src, "<relevant-expr>", "eval")

    def relevant(pending_offer, gate_enabled, is_addressed):
        return eval(code, {}, {
            "pending_offer": pending_offer,
            "gate_enabled": gate_enabled,
            "is_addressed": is_addressed,
        })

    # pending_offer is the real runtime value: either None (no offer) or a
    # truthy object (dict/etc) returned by get_pending_offer() — not a bool.
    NO_OFFER = None
    AN_OFFER = {"action": "turn_off_kitchen_lights"}

    # Requirement 1: irrelevant ambient speech (no offer, gate on, not addressed)
    assert relevant(NO_OFFER, True, False) is False

    # Requirement 2: ordinary accepted command (no offer, gate on, addressed)
    assert relevant(NO_OFFER, True, True) is True

    # Requirements 3 & 4: a reply to a pending offer stays relevant regardless
    # of whether it reads as addressed to Nova — covers both "yes"/"no"
    # (accept/reject) and anything else said while an offer is pending.
    assert relevant(AN_OFFER, True, False) is True
    assert relevant(AN_OFFER, True, True) is True

    # Requirement 5: disabling the relevance gate preserves persistence even
    # for input that wouldn't otherwise look addressed to Nova.
    assert relevant(NO_OFFER, False, False) is True
    assert relevant(NO_OFFER, False, True) is True

    # Remaining combinations, for completeness of the truth table.
    assert relevant(AN_OFFER, False, False) is True
    assert relevant(AN_OFFER, False, True) is True


# ── 3. No formula drift between the persistence gate and the routing gate ────

def test_is_addressed_to_nova_is_computed_exactly_once():
    # Only the definition (line ~152) and the single computation site should
    # call it — the later routing-only gate must reuse `is_addressed`, not
    # recompute it, so the two checks can never disagree.
    calls = SRC.count("_is_addressed_to_nova(user_input.text)")
    assert calls == 1, (
        f"expected exactly one call site for _is_addressed_to_nova(...), found {calls} — "
        f"the routing gate below the offer block must reuse `is_addressed`, not recompute it"
    )


def test_routing_gate_reuses_precomputed_gate_enabled_and_is_addressed():
    assert "if gate_enabled and not is_addressed:" in SRC
    # The old, since-removed recomputation must not have come back.
    assert 'self._opt("relevance_gate", True) and not _is_addressed_to_nova(' not in SRC


def test_routing_gate_is_still_reachable_after_persistence_and_offer_block():
    # This is the one case a naive "just move persistence below the gate"
    # would have broken differently: a pending offer keeps the turn relevant
    # (so it persists), but a reply that's neither accept/decline nor
    # addressed to Nova must still fail to ROUTE anywhere. The routing gate
    # must sit after both the persistence writes and the offer short-circuit.
    persistence_idx = _index('save_message, "user", user_input.text, cid, episodic_subject)')
    offer_shortcircuit_idx = _index("if offer_reply is not None:")
    routing_gate_idx = _index("if gate_enabled and not is_addressed:")
    assert offer_shortcircuit_idx > persistence_idx
    assert routing_gate_idx > offer_shortcircuit_idx


# ── 4. Pending-offer persistence (requirements 3 & 4) ─────────────────────────

def test_offer_short_circuit_returns_after_persistence_writes():
    # A pending-offer accept/decline reply must still be persisted before its
    # own short-circuit return — i.e. persistence for THIS turn happens
    # before the "Done"/"Understood" canned reply is sent.
    persistence_idx = _index('save_message, "user", user_input.text, cid, episodic_subject)')
    offer_return_idx = SRC.find(
        "return conversation.ConversationResult(response=ir, conversation_id=cid)",
        _index("if offer_reply is not None:"),
    )
    assert offer_return_idx > persistence_idx


def test_get_pending_offer_is_called_exactly_once():
    # Single read, captured before the relevance decision — the offer
    # handler below must reuse that same object, not read the pending-offer
    # state a second time.
    calls = SRC.count(".get_pending_offer(")
    assert calls == 1, (
        f"expected exactly one call to get_pending_offer(...), found {calls} — "
        f"the offer handler must reuse the object captured before the "
        f"relevance decision, not read pending-offer state again"
    )


def test_pending_offer_captured_before_the_decision_has_an_exception_fallback():
    assert "pending_offer = cognitive_core.get_pending_offer()" in SRC
    assert "pending_offer = None" in SRC  # exception fallback
    # Must be captured BEFORE the decision, not after.
    assert _index("pending_offer = cognitive_core.get_pending_offer()") < _index("if not relevant:")


def test_pending_offer_capture_is_fail_open_including_the_import_itself():
    # Both the import AND the read are inside the same try — an import
    # failure (not just a get_pending_offer() failure) must also fall
    # through cleanly, since the later offer handler references
    # `cognitive_core` too. A companion PHACC test
    # (test_pending_offer_lookup_failure_does_not_break_an_addressed_command)
    # proves this behaviorally: an ordinary command still persists and
    # routes when the lookup raises.
    capture_block = SRC[_index("try:\n            from . import cognitive_core"):_index("gate_enabled = self._opt")]
    assert "from . import cognitive_core" in capture_block
    assert "pending_offer = cognitive_core.get_pending_offer()" in capture_block
    assert "except Exception:" in capture_block
    assert "cognitive_core = None" in capture_block
    assert "pending_offer = None" in capture_block


def test_offer_handler_reuses_the_captured_pending_offer_object():
    # The later handler must branch on the SAME `pending_offer` name, not a
    # fresh local (e.g. the old `pending = cognitive_core.get_pending_offer()`
    # pattern must not come back).
    assert "if pending_offer:" in SRC
    assert "pending = cognitive_core.get_pending_offer()" not in SRC


# ── 5. Dedup and presence-gate paths are untouched (requirement 6) ───────────

def test_dedup_early_return_unchanged_and_precedes_the_relevance_decision():
    dedup_marker = "is_dup, cached = _check_and_claim_dedup(user_input.text, device_id)"
    gate_idx = _index("if not relevant:")
    assert _index(dedup_marker) < gate_idx
    # The dedup path must still return before conversation_id/cid is even
    # computed — untouched by this change.
    assert _index(dedup_marker) < _index(
        'cid       = user_input.conversation_id or user_input.device_id or "default"'
    )


def test_presence_gate_early_return_unchanged_and_precedes_the_relevance_decision():
    presence_marker = 'if device_id and self._opt("presence_gate", False):'
    gate_idx = _index("if not relevant:")
    assert _index(presence_marker) < gate_idx
    assert _index(presence_marker) < _index(
        'cid       = user_input.conversation_id or user_input.device_id or "default"'
    )
