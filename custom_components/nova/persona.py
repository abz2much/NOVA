"""Nova's voice: stock phrases chosen with a little variety, and the
honorific lead-in used by the senders.

One character throughout (dry, precise, deferential) that gets plainer as
the matter gets more serious: the urgent and grave registers never receive
the witty lines. The picker keeps a short memory per selection key so the
same line is not repeated straight away.

Registers are "light", "neutral", "urgent" and "grave". In a template,
``{H}`` is the honorific with its first letter capitalised and ``{h}`` is
the honorific as given (whitespace stripped).

This is a stdlib-only leaf. Nova modules are imported lazily inside the
functions that need them, so the file also loads on its own, outside the
package.
"""
from __future__ import annotations

import bisect
import random
import string
import time
from collections import deque
from typing import Optional

# ── tunables (read at call time; tests replace these) ────────────────────────

_VARIETY: bool = True   # False: always the first eligible line
_BANTER: int = 1        # 0 plain, 1 dry, 2 full wit
_recent: dict[str, deque] = {}   # selection key -> recent candidate indices


# ── phrase pools ─────────────────────────────────────────────────────────────
# The order is significant: with variety off the first eligible entry wins.

_ACK: dict[str, list[str]] = dict(
    light=[
        "Of course, {h}.",
        "Right away, {h}.",
        "Consider it done.",
        "On it, {h}.",
        "Certainly.",
        "As you wish, {h}.",
        "Happily, {h}.",
        "Very good, {h}.",
        "Straight away.",
        "But of course, {h}.",
    ],
    neutral=[
        "Of course, {h}.",
        "Right away, {h}.",
        "Certainly, {h}.",
        "Consider it done.",
        "At once, {h}.",
        "Very good, {h}.",
        "As you wish, {h}.",
    ],
    urgent=[
        "At once, {h}.",
        "Right away.",
        "On it.",
        "Immediately, {h}.",
        "Done.",
    ],
)

_DONE: dict[str, list[str]] = dict(
    light=[
        "Done, {h}.",
        "That's handled.",
        "All set, {h}.",
        "Taken care of.",
        "There we are.",
        "Complete, {h}.",
        "Sorted.",
        "That's done, {h}.",
    ],
    neutral=[
        "Done, {h}.",
        "That's complete, {h}.",
        "Handled, {h}.",
        "All set.",
        "Taken care of, {h}.",
    ],
    urgent=[
        "Done.",
        "Handled, {h}.",
        "Complete.",
    ],
)

_GREET: dict[str, list[str]] = dict(
    morning=[
        "Good morning, {h}.",
        "Morning, {h}.",
        "A good morning to you, {h}.",
        "Good morning. I trust you slept well, {h}.",
        "Good morning.",
    ],
    afternoon=[
        "Good afternoon, {h}.",
        "Afternoon, {h}.",
        "Good afternoon to you, {h}.",
        "Good afternoon.",
    ],
    evening=[
        "Good evening, {h}.",
        "Evening, {h}.",
        "Good evening to you, {h}.",
        "Good evening.",
    ],
    night=[
        "Working late, {h}?",
        "Good evening, {h}.",
        "Burning the midnight oil, {h}?",
        "Still up, {h}? Good evening.",
        "Burning the midnight oil?",
    ],
)

# Each register carries at least one opener without an honorific.
_OPENER: dict[str, list[str]] = dict(
    neutral=[
        "{H},",
        "For your awareness, {h} —",
        "A small matter, {h} —",
        "If I may, {h} —",
        "Just so you know, {h} —",
        "A note, {h} —",
        "Worth mentioning, {h} —",
        "For your awareness —",
        "Worth mentioning —",
        "A quick note —",
    ],
    urgent=[
        "{H}, your attention —",
        "{H}, you should know —",
        "{H}, if I may —",
        "{H}, a moment —",
        "{H} —",
        "Your attention, please —",
        "A moment, please —",
    ],
    grave=[
        "{H}.",
        "{H} —",
        "Attention.",
        "Listen.",
    ],
)

_WORKING: dict[str, list[str]] = dict(
    neutral=[
        "One moment, {h}.",
        "Looking into it.",
        "Working on it, {h}.",
        "Just a moment.",
        "Allow me a moment, {h}.",
        "Let me see, {h}.",
    ],
)

_UNABLE: dict[str, list[str]] = dict(
    neutral=[
        "I'm afraid I can't do that, {h}.",
        "That's beyond me at the moment, {h}.",
        "I'm not able to manage that just yet, {h}.",
        "Regrettably, {h}, that's outside what I can do.",
        "I wish I could, {h}, but that's not within my reach.",
        "I'm afraid I can't do that.",
        "That's beyond me at the moment.",
    ],
)

# Full-banter overlays: only ever added at banter level 2.
_ACK_FULL: list[str] = [
    "As you wish, {h}.",
    "Consider it already done, {h}.",
    "With pleasure, {h}.",
    "A fine idea, {h}. On it.",
    "Naturally, {h}.",
    "Say no more, {h}.",
    "Ever your instrument, {h}.",
    "At once — I do live to serve, {h}.",
]

_DONE_FULL: list[str] = [
    "Done, {h} — effortless, as ever.",
    "Handled. You're welcome, {h}.",
    "There we are. I do enjoy being useful, {h}.",
    "Complete, {h}. Try not to look so surprised.",
    "Taken care of. I'll add it to my considerable tally, {h}.",
    "All set, {h}. Another small triumph.",
]

_UNABLE_FULL: list[str] = [
    "I'm afraid that's beyond even my talents, {h} — and they are not modest.",
    "Would that I could, {h}. Alas, I have limits. Few, but real.",
    "That one's outside my remit, {h}. I'd rather decline than overpromise.",
    "I must decline, {h} — not from reluctance, from incapacity.",
]

_ASIDES: list[str] = [
    "Will there be anything else, {h}?",
    "A pleasure, as always.",
    "I'll be here. I'm always here.",
]

# Hour boundaries at which the daypart changes (the later bucket starts
# at the boundary), and the dayparts between them.
_DAYPART_EDGES = (5, 12, 17, 22)
_DAYPARTS = ("night", "morning", "afternoon", "evening", "night")

_PLACEHOLDERS = ("{H}", "{h}")


# ── settings ─────────────────────────────────────────────────────────────────

def set_variety(on) -> None:
    global _VARIETY
    _VARIETY = bool(on)


def set_banter(level) -> None:
    global _BANTER
    try:
        wanted = int(level)
    except Exception:
        wanted = 1
    _BANTER = max(0, min(2, wanted))


def _banter_from_cfg() -> None:
    """Refresh the banter level: the active mode first, then config.
    When neither supplies a value the current level is kept."""
    try:
        from . import modes
        level = modes.mode_banter_level()
        if level is not None:
            set_banter(level)
            return
    except Exception:
        pass
    try:
        from . import nova_config
        value = nova_config.get("banter_level", None)
        if value is not None:
            set_banter(value)
    except Exception:
        pass


# ── selection ────────────────────────────────────────────────────────────────

def _clean(honorific) -> str:
    return (honorific or "").strip()


def _has_placeholder(template: str) -> bool:
    return any(mark in template for mark in _PLACEHOLDERS)


def capitalise(honorific) -> str:
    """The honorific with each word capitalised. An apostrophe does not
    start a new word: "ma'am" gives "Ma'am", never "Ma'Am"."""
    return string.capwords(_clean(honorific))


def _fill(template: str, honorific) -> str:
    h = _clean(honorific)
    upper = h[:1].upper() + h[1:]
    return template.replace("{H}", upper).replace("{h}", h)


def _pick(pool: list, key: str, honorific) -> str:
    if not pool:
        return ""
    candidates = list(pool)
    memory_key = key
    if not _clean(honorific):
        bare = [t for t in pool if not _has_placeholder(t)]
        if bare:
            candidates = bare
            if len(bare) != len(pool):
                memory_key = key + ":bare"

    if not _VARIETY:
        return _fill(candidates[0], honorific)

    seen = _recent.get(memory_key)
    if seen is None:
        seen = deque(maxlen=min(3, max(1, len(candidates) - 1)))
        _recent[memory_key] = seen
    eligible = [i for i in range(len(candidates)) if i not in seen]
    if not eligible:
        eligible = list(range(len(candidates)))
    choice = random.choice(eligible)
    seen.append(choice)
    return _fill(candidates[choice], honorific)


def _reg(pools: dict, register: str) -> list:
    chosen = pools.get(register)
    if chosen:
        return chosen
    fallback = pools.get("neutral")
    if fallback:
        return fallback
    return next(iter(pools.values()), [])


def _banter_pool(base_pools: dict, register: str, full_overlay: list) -> list:
    pool = list(_reg(base_pools, register))
    if _BANTER >= 2 and register == "light" and full_overlay:
        pool.extend(full_overlay)
    return pool


def register_for(urgency: Optional[str]) -> str:
    level = urgency.lower() if urgency else ""
    if level == "critical":
        return "grave"
    if level == "high":
        return "urgent"
    return "neutral"


# ── speech acts ──────────────────────────────────────────────────────────────

def acknowledge(honorific: str = "sir", register: str = "neutral") -> str:
    _banter_from_cfg()
    pool = _banter_pool(_ACK, register, _ACK_FULL)
    return _pick(pool, f"ack:{register}:{_BANTER}", honorific)


def completed(honorific: str = "sir", register: str = "neutral") -> str:
    _banter_from_cfg()
    pool = _banter_pool(_DONE, register, _DONE_FULL)
    return _pick(pool, f"done:{register}:{_BANTER}", honorific)


def working(honorific: str = "sir") -> str:
    return _pick(_WORKING["neutral"], "working", honorific)


def unable(honorific: str = "sir") -> str:
    _banter_from_cfg()
    extra = _UNABLE_FULL if _BANTER >= 2 else []
    return _pick([*_UNABLE["neutral"], *extra], f"unable:{_BANTER}", honorific)


def aside(honorific: str = "sir") -> str:
    _banter_from_cfg()
    if _BANTER < 2:
        return ""
    return _pick(_ASIDES, "aside", honorific)


def greeting(honorific: str = "sir", hour: Optional[int] = None) -> str:
    if hour is None:
        hour = time.localtime().tm_hour
    part = _DAYPARTS[bisect.bisect_right(_DAYPART_EDGES, hour)]
    return _pick(_GREET[part], f"greet:{part}", honorific)


def announce_opener(honorific: str = "sir", register: str = "neutral") -> str:
    return _pick(_reg(_OPENER, register), f"open:{register}", honorific)


def lead_in(honorific: str, sentence: str) -> str:
    """Put the honorific in front of a sentence, or, with no honorific,
    capitalise the sentence's first character."""
    if not sentence:
        return sentence
    h = _clean(honorific)
    if h:
        return f"{capitalise(h)}, {sentence}"
    return sentence[:1].upper() + sentence[1:]
