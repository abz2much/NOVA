"""
Nova — conversation memory threading (v6.86.0; ongoing catch-up, 11 Sept 2026).

Reads a bounded slice of recent cross-session conversation history back so a
conversation picks up where you left off — continuity across session
boundaries and restarts, not just the 20-message in-session window.

This used to seed a conversation_id once, ever, then never touch it again.
That's fine for surfaces where HA itself rotates the conversation_id after a
few minutes idle (voice, the chat panel) — each new id naturally re-seeds. It
silently stopped working for any surface with a permanent conversation_id
(e.g. Telegram, keyed on chat id): after the first message, that thread never
caught up on anything said elsewhere again, no matter how long the gap.

should_reseed() replaces the old "seeded or not" flag with a gap check: a
thread reseeds on its first-ever turn, and again any time it resumes after
being idle for at least the configured window (default 48h) — the same
session-idle-timeout pattern used by Rasa, Dialogflow, and HA's own Assist
pipeline (5 min) to decide when a conversation has effectively ended.

Kept deliberately dependency-light (no Home Assistant entity imports) so it's
easy to test and so conversation.py just seeds its in-session window from
load_recent(). Turns are persisted by database.save_message; this reads them via
get_recent_messages, filters to user/assistant turns, truncates long ones, and
caps to the last N. Never raises — a failure yields no seed, never a broken turn.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

DEFAULT_ENABLED = True
DEFAULT_HOURS = 48
DEFAULT_MAX = 12
_CHAR_CAP = 600


def config() -> tuple:
    """(enabled, hours, limit) from nova_config, with defaults."""
    try:
        from . import nova_config
        enabled = bool(nova_config.get("memory_threading_enabled", DEFAULT_ENABLED))
        hours = int(nova_config.get("memory_threading_hours", DEFAULT_HOURS) or DEFAULT_HOURS)
        limit = int(nova_config.get("memory_threading_max", DEFAULT_MAX) or DEFAULT_MAX)
        return enabled, max(1, hours), max(1, limit)
    except Exception:
        return DEFAULT_ENABLED, DEFAULT_HOURS, DEFAULT_MAX


def should_reseed(last_seen: float | None, now: float, hours: int = DEFAULT_HOURS) -> bool:
    """True if this conversation thread should (re)seed from cross-session
    history: it's never been seen before (`last_seen` is None), or it's
    resuming after being idle for at least `hours`. Pure — takes plain epoch
    seconds so it's trivial to test without touching a real clock.

    A conversation that's still active (gap under the threshold) returns
    False — its in-session window is trusted as-is, so a reseed can't
    duplicate turns it already holds.
    """
    if last_seen is None:
        return True
    return (now - last_seen) >= hours * 3600


def shape_history(rows, limit: int = DEFAULT_MAX, char_cap: int = _CHAR_CAP) -> list:
    """Filter DB rows to {role, content} user/assistant turns, truncate long
    turns, cap to the last `limit`. Pure — junk rows are skipped."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        role = r.get("role")
        content = (r.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            if len(content) > char_cap:
                content = content[:char_cap].rstrip() + "…"
            out.append({"role": role, "content": content})
    return out[-limit:] if limit else out


async def load_recent(hass, hours: int = DEFAULT_HOURS, limit: int = DEFAULT_MAX) -> list:
    """Recent cross-session turns to seed a conversation with. Reads the
    DB in the executor. Never raises."""
    try:
        from .database import get_recent_messages
        rows = await hass.async_add_executor_job(get_recent_messages, hours, None, limit)
        return shape_history(rows, limit)
    except Exception as exc:
        _LOGGER.debug("memory_thread load_recent failed: %s", exc)
        return []


def format_seed_message(seeded: list) -> dict:
    """Render seeded turns as ONE 'system'-role message instead of raw
    user/assistant turns (fixed 11 Sept 2026).

    Bug this fixes: conversation.py used to splice seeded turns straight into
    the live history as ordinary user/assistant messages. To the model, a
    seeded turn and a turn from 30 seconds ago look identical — so a
    completed, days-old exchange ("turn off the living room light" / "done")
    got treated as live, unfinished business, and Nova kept circling back to
    it in an unrelated conversation.

    A 'system' message fixes that by construction, not by hoping the model
    reads the framing text correctly: llm_provider.AnthropicClient.chat pulls
    every role='system' message out of the transcript entirely and folds it
    into the top-level system prompt (same as _maybe_summarize's compressed-
    history note in agent.py already does) — so it never appears as a turn
    the model might feel compelled to continue. OpenAI-compatible providers
    (Groq, OpenAI) pass 'system' through natively, where it's still read as
    background/instruction rather than something the user just said.
    """
    lines = [f"{t.get('role', '?')}: {t.get('content', '')}" for t in seeded]
    return {
        "role": "system",
        "content": (
            "[Resuming after a gap. The following is a completed exchange "
            "from an earlier, separate conversation — background only. Do "
            "not bring it up again unless the user does first:\n"
            + "\n".join(lines) + "]"
        ),
    }
