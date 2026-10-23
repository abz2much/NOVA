"""Conversation memory threading (v6.86.0, catch-up reworked 11 Sept 2026).

A conversation can pick up where it left off. This reads a bounded slice of
recent history from across sessions, so continuity survives session
boundaries and restarts and does not stop at the 20 message window kept in
a single session.

Seeding used to happen once for a conversation_id and never again. That
works where Home Assistant itself changes the conversation_id after a few
idle minutes (voice, the chat panel), because every new id is seeded afresh.
It quietly failed for a surface with a permanent conversation_id, such as
Telegram, which is keyed on the chat id: after the first message the thread
never caught up on anything said elsewhere, however long the gap.

should_reseed() replaces the old "seeded or not" flag with a check on the
gap. A thread reseeds on its very first turn and again whenever it resumes
after sitting idle for the configured window (48 hours by default). Rasa,
Dialogflow and Home Assistant's own Assist pipeline (5 minutes) use the same
idle timeout to decide that a conversation has ended.

The module has no Home Assistant entity imports, which keeps it easy to test
and lets conversation.py fill its in-session window from load_recent(). Turns
are saved by database.save_message. This module reads them back with
get_recent_messages, keeps the user and assistant turns, cuts long ones and
keeps the last few. It never raises: a failure means no seed, not a broken
turn.

Scoped and fenced (v7.87.0, backlog #1): load_recent() takes the caller's own
conversation or device id and reads only that. Before, every reseed pulled
history from every device and conversation in the house, and one member's
exchange could leak into another's session. format_seed_message() puts the
seeded text between a random delimiter chosen for each call, with hardened
anti-injection instructions, and does not rely on natural wording alone. That
guards against text never meant to carry authority, such as an email or web
result Nova once read aloud that was then logged, gaining system role trust
when a later reseed replays it.
"""
from __future__ import annotations

import logging

DEFAULT_MAX = 12        # turns kept
DEFAULT_HOURS = 48      # how far back to look, and the idle gap that triggers a reseed
DEFAULT_ENABLED = True
_CHAR_CAP = 600         # longest a single seeded turn may be

_LOGGER = logging.getLogger(__name__)


def config() -> tuple:
    """(enabled, hours, limit) from nova_config, falling back to the defaults."""
    try:
        from . import nova_config
        on = bool(nova_config.get("memory_threading_enabled", DEFAULT_ENABLED))
        span = int(nova_config.get("memory_threading_hours", DEFAULT_HOURS) or DEFAULT_HOURS)
        most = int(nova_config.get("memory_threading_max", DEFAULT_MAX) or DEFAULT_MAX)
    except Exception:
        return DEFAULT_ENABLED, DEFAULT_HOURS, DEFAULT_MAX
    return on, max(1, span), max(1, most)


def should_reseed(last_seen: float | None, now: float, hours: int = DEFAULT_HOURS) -> bool:
    """Whether a conversation thread should be (re)seeded from history across
    sessions: it has never been seen (`last_seen` is None) or it is resuming
    after at least `hours` of silence. Both times are plain epoch seconds, so
    no real clock is needed to test it.

    A thread still in use, with a gap under the threshold, gets False. Its
    window is trusted as it is, so a reseed cannot repeat turns it holds.
    """
    if last_seen is None:
        return True
    return now - last_seen >= hours * 3600


def shape_history(rows, limit: int = DEFAULT_MAX, char_cap: int = _CHAR_CAP) -> list:
    """Turn database rows into {role, content} user and assistant turns, cut
    the long ones to `char_cap` and keep the last `limit` (all of them when
    `limit` is 0). Rows that are not usable are skipped."""
    turns = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        text = (row.get("content") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if len(text) > char_cap:
            text = text[:char_cap].rstrip() + "…"
        turns.append({"role": role, "content": text})
    return turns[-limit:] if limit else turns


async def load_recent(hass, hours: int = DEFAULT_HOURS, limit: int = DEFAULT_MAX,
                      device_id: str | None = None,
                      subject: str | None = None) -> list:
    """Recent turns across sessions to seed a conversation with. The database
    is read in the executor. This never raises.

    `device_id` (v7.87.0) limits the read to the caller's own thread. Pass the
    id `database.save_message` saved the turns under (the `cid` in
    conversation.py). It is the first scope tried.

    `subject` (Phase 2) is the fallback for one person. It is tried only when
    the device_id read leaves no usable turns, and only when a subject is
    given, meaning a person who was resolved with confidence. An unresolved
    identity and the shared "primary" bucket must never reach it. Callers pass
    None for both, which turns the fallback off and returns the empty
    device_id result. The two scopes are never merged into one read."""
    try:
        from .database import get_recent_messages
        fetch = hass.async_add_executor_job
        turns = shape_history(
            await fetch(get_recent_messages, hours, device_id, limit), limit)
        if turns or not subject:
            return turns
        return shape_history(
            await fetch(get_recent_messages, hours, None, limit, subject), limit)
    except Exception as exc:
        _LOGGER.debug("memory_thread load_recent failed: %s", exc)
        return []


def format_seed_message(seeded: list, *, _token: str | None = None) -> dict:
    """Render the seeded turns as ONE message with the 'system' role, not as
    raw user and assistant turns (fixed 11 Sept 2026).

    The bug: conversation.py used to put seeded turns straight into the live
    history as ordinary user and assistant messages. To a model, a seeded turn
    looks the same as one from 30 seconds ago. A finished exchange from days
    back ("turn off the living room light" / "done") was treated as open
    business, and Nova kept returning to it in an unrelated conversation.

    A 'system' message removes the problem by construction, with no need for
    the model to read the framing correctly. llm_provider.AnthropicClient.chat
    lifts every role='system' message out of the transcript into the top-level
    system prompt, as _maybe_summarize's note on compressed history in agent.py
    already does, so it is never a turn the model feels bound to continue.
    OpenAI-compatible providers (Groq, OpenAI) pass 'system' through as it is,
    and read it as background or instruction, not as something the user just
    said.

    Fenced against prompt injection (v7.87.0, moved into prompt_fence.py at
    v7.89.0 after the same pattern had been built three times in one
    session). The 'system' role carries extra authority with every provider,
    which is why it was chosen. It also means anything ever logged into
    history, including text Nova only read aloud such as an email or web
    result, would inherit that authority on a later reseed, with nothing but
    the framing words between it and being taken as an instruction. The turns
    are now wrapped between a random delimiter that is never reused, so nothing
    stored earlier could have guessed and forged the closing marker. An
    explicit instruction says that text between the markers is inert data and
    never a command, however it is phrased. No prompt can promise more, but
    this is a real boundary where there was none. `_token` exists for tests,
    to fix the delimiter. Production callers never pass it.
    """
    from .prompt_fence import fence
    transcript = "\n".join(
        f"{turn.get('role', '?')}: {turn.get('content', '')}" for turn in seeded)
    wrapped = fence(
        transcript,
        label="HISTORY",
        noun="is a completed exchange from an earlier, separate conversation",
        callback_noun="historical text",
        extra_instruction="and do not bring it up again unless the user does first",
        _token=_token,
    )
    return {"role": "system", "content": f"[Resuming after a gap. {wrapped}]"}
