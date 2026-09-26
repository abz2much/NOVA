"""
Nova Local Intent Engine (v5.7.00).

PRIMARY handler for all conversations and event reasoning. Zero API calls.
Handles 95%+ of user requests locally with pattern matching, HA entity
resolution, contextual state queries, multi-entity commands, scene/script
activation, media control, and conversational responses.

Only genuinely complex or ambiguous requests fall through to Groq/Gemini.

Architecture:
  1. Regex intent matching (commands, queries, greetings)
  2. Contextual multi-entity resolution (area-based, group, "all X")
  3. HA state introspection (who's home, what's open, etc.)
  4. Scene/script/automation triggers
  5. Media player control
  6. Complexity scoring (decides local vs LLM escalation)
  7. Follow-up context tracking
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Rolling memory of the last few acknowledgment lead-ins used, so Nova does
# not repeat the same opener twice in a row — variety without randomness that
# feels chaotic. Nova rarely says the same confirmation back-to-back.
_recent_acks: list[str] = []


def _pick(options: list[str]) -> str:
    """
    Choose a phrasing that wasn't just used. Keeps confirmations from sounding
    canned while staying deterministic-enough (no external state, no cost).
    """
    if not options:
        return ""
    fresh = [o for o in options if o not in _recent_acks]
    choice = random.choice(fresh) if fresh else random.choice(options)
    _recent_acks.append(choice)
    if len(_recent_acks) > 4:
        _recent_acks.pop(0)
    return choice


# ── Result types ────────────────────────────────────────────────────────────

@dataclass
class LocalResult:
    """Result of local intent execution."""
    text: str
    success: bool
    handled: bool = True   # False = couldn't handle, fall through to LLM
    # Set only by the deterministic "repeat that" command below — lets the
    # conversation-reply path (conversation.py) attribute a Spoken History
    # entry back to the original it repeated, but ONLY when that reply ends
    # up going through a confirmed-delivery path (async_announce). An
    # ordinary Assist reply routed through the pipeline's own TTS, where
    # Nova cannot observe whether speech happened, is never recorded.
    repeat_of_id: Optional[int] = None


# ── Follow-up context ──────────────────────────────────────────────────────

class _ConvCtx:
    last_entity: str = ""
    last_area: str = ""
    last_domain: str = ""
    last_action: str = ""
    last_ts: float = 0.0

_CTX = _ConvCtx()

def _update_ctx(**kw):
    for k, v in kw.items():
        if v:
            setattr(_CTX, f"last_{k}", v)
    _CTX.last_ts = time.time()

def _ctx_fresh():
    return (time.time() - _CTX.last_ts) < 120


# ── Normalization ───────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[.,!?;:]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


# ── Intent patterns ─────────────────────────────────────────────────────────

_INTENT_PATTERNS = [
    # Lights
    (r"turn\s+on\s+(?:the\s+)?(.+?)(?:\s+light(?:s)?)?$",       "turn_on",  "light"),
    (r"turn\s+off\s+(?:the\s+)?(.+?)(?:\s+light(?:s)?)?$",      "turn_off", "light"),
    (r"switch\s+on\s+(?:the\s+)?(.+?)(?:\s+light(?:s)?)?$",     "turn_on",  "light"),
    (r"switch\s+off\s+(?:the\s+)?(.+?)(?:\s+light(?:s)?)?$",    "turn_off", "light"),
    (r"lights?\s+on\s+(?:in\s+)?(?:the\s+)?(.+)$",              "turn_on",  "light"),
    (r"lights?\s+off\s+(?:in\s+)?(?:the\s+)?(.+)$",             "turn_off", "light"),
    (r"dim\s+(?:the\s+)?(.+?)(?:\s+light(?:s)?)?\s+to\s+(\d+)", "dim",      "light"),
    (r"set\s+(?:the\s+)?(.+?)(?:\s+light(?:s)?)?\s+(?:brightness\s+)?to\s+(\d+)",
     "dim", "light"),
    (r"brighten\s+(?:the\s+)?(.+)", "brighten", "light"),
    # Switches
    (r"turn\s+on\s+(?:the\s+)?(.+?)(?:\s+switch)?$",   "turn_on",  "switch"),
    (r"turn\s+off\s+(?:the\s+)?(.+?)(?:\s+switch)?$",  "turn_off", "switch"),
    # Locks (unlock before lock; lock's (?<!un) stops it matching inside "unlock")
    (r"unlock\s+(?:the\s+)?(.+?)(?:\s+door)?$", "unlock", "lock"),
    (r"(?<!un)lock\s+(?:the\s+)?(.+?)(?:\s+door)?$",   "lock",   "lock"),
    # Covers / Garage
    (r"open\s+(?:the\s+)?(.+?)(?:\s+(?:door|gate|cover|garage))?$",  "open",  "cover"),
    (r"close\s+(?:the\s+)?(.+?)(?:\s+(?:door|gate|cover|garage))?$", "close", "cover"),
    # Climate
    (r"set\s+(?:the\s+)?(?:temperature|thermostat|temp)\s+(?:to|at)\s+(\d+)",
     "set_temp", "climate"),
    (r"set\s+(?:the\s+)?(.+?)\s+(?:temperature|thermostat|temp)\s+(?:to|at)\s+(\d+)",
     "set_temp_named", "climate"),
    (r"(?:make\s+it|set\s+it)\s+(?:to\s+)?(\d+)\s*(?:degrees|°)?", "set_temp", "climate"),
    # Fan
    (r"turn\s+on\s+(?:the\s+)?(.+?)(?:\s+fan)?$",   "turn_on",  "fan"),
    (r"turn\s+off\s+(?:the\s+)?(.+?)(?:\s+fan)?$",  "turn_off", "fan"),
    # Media
    (r"(?:pause|stop)\s+(?:the\s+)?(?:music|media|tv|playback)(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "media_pause", "media_player"),
    (r"(?:resume|play|unpause)\s+(?:the\s+)?(?:music|media|tv|playback)(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "media_play", "media_player"),
    (r"(?:volume\s+up|turn\s+(?:it|the\s+volume)\s+up)(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "volume_up", "media_player"),
    (r"(?:volume\s+down|turn\s+(?:it|the\s+volume)\s+down)(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "volume_down", "media_player"),
    (r"(?:set\s+)?volume\s+(?:to\s+)?(\d+)(?:\s+(?:percent|%?))?(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "volume_set", "media_player"),
    (r"mute(?:\s+(?:the\s+)?(?:tv|speaker|media))?(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "mute", "media_player"),
    (r"unmute(?:\s+(?:the\s+)?(?:tv|speaker|media))?(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "unmute", "media_player"),
    (r"(?:skip|next)\s+(?:track|song)(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$",
     "media_next", "media_player"),
    # Scenes/scripts
    (r"(?:activate|run|trigger|start|execute)\s+(?:the\s+)?(.+?)(?:\s+scene)?$",
     "scene", None),
    # State queries
    (r"(?:what(?:'s| is)\s+the\s+)?temperature\s+(?:in\s+)?(?:the\s+)?(.+)",
     "query_temp", None),
    (r"(?:what(?:'s| is)\s+the\s+)?(.+?)(?:\s+temperature)", "query_temp", None),
    (r"(?:is|are)\s+(?:the\s+)?(.+?)\s+(?:on|off|open|closed|locked|unlocked)",
     "query_state", None),
    (r"(?:what(?:'s| is)\s+the\s+)?status\s+of\s+(?:the\s+)?(.+)",
     "query_state", None),
    # Time/Date
    (r"what\s+time\s+is\s+it",      "query_time", None),
    (r"what(?:'s| is)\s+the\s+time", "query_time", None),
    (r"what(?:'s| is)\s+today(?:'s)?\s+date", "query_date", None),
    # Greetings
    (r"^(?:hey|hi|hello|good\s+(?:morning|afternoon|evening))(?:\s+nova)?[.!]?$",
     "greeting", None),
    (r"^(?:thanks|thank\s+you|cheers)(?:\s+.*)?$", "thanks", None),
    (r"^(?:you'?re\s+welcome|no\s+problem|no\s+worries)", "thanks", None),
    # Home status
    (r"(?:home|house)\s+status",     "status", None),
    (r"how(?:'s| is)\s+the\s+house", "status", None),
    (r"system\s+status",             "status", None),
    (r"run\s+(?:a\s+)?diagnostic",   "status", None),
]

# ── Multi-entity / bulk patterns ────────────────────────────────────────────

_BULK_PATTERNS = [
    (r"turn\s+off\s+(?:all\s+)?(?:the\s+)?lights?$", "turn_off", "light", None),
    (r"turn\s+on\s+(?:all\s+)?(?:the\s+)?lights?$",  "turn_on",  "light", None),
    (r"turn\s+off\s+(?:all\s+)?(?:the\s+)?lights?\s+(?:in|on)\s+(?:the\s+)?(.+)$",
     "turn_off", "light", "area"),
    (r"turn\s+on\s+(?:all\s+)?(?:the\s+)?lights?\s+(?:in|on)\s+(?:the\s+)?(.+)$",
     "turn_on", "light", "area"),
    (r"unlock\s+(?:all\s+)?(?:the\s+)?doors?$", "unlock", "lock", None),
    (r"(?<!un)lock\s+(?:all\s+)?(?:the\s+)?doors?$",   "lock",   "lock", None),
    (r"close\s+(?:all\s+)?(?:the\s+)?(?:covers?|blinds?|shades?)$", "close", "cover", None),
    (r"open\s+(?:all\s+)?(?:the\s+)?(?:covers?|blinds?|shades?)$",  "open",  "cover", None),
    (r"turn\s+off\s+(?:all\s+)?(?:the\s+)?fans?$", "turn_off", "fan", None),
    (r"turn\s+on\s+(?:all\s+)?(?:the\s+)?fans?$",  "turn_on",  "fan", None),
    (r"turn\s+(?:off|out)\s+everything$", "turn_off", "all", None),
]

# ── Contextual queries ──────────────────────────────────────────────────────

_HOME = r"(?:at\s+)?home\b(?!\s*assistant)"
_NOW = r"(?:currently\s+|still\s+|right\s+now\s+)?"
_PLATFORM_HEALTH = (
    r"^(?=.*\bhome\s?assistant\b)(?=.*\b(?:reach|reachable|connect|connected|connection|access|"
    r"available|online|offline|up(?!\s+to\s+date)|down|running|responding|respond|working|"
    r"alive|status|health|healthy|talk\s+to)\b)")

_QUERY_PATTERNS = [
    (r"(?:repeat\s+that|say\s+that\s+again|what\s+did\s+you\s+just\s+say|"
     r"repeat\s+(?:your\s+)?last\s+announcement)\b",       "repeat_last"),
    # "Home Assistant" is the platform, never a presence question: a
    # question about reaching or the health of Home Assistant gets a status
    # answer from live evidence, and every presence pattern below refuses
    # "home" when "assistant" follows it.
    (_PLATFORM_HEALTH,                                   "platform_status"),
    # Presence needs presence grammar: who is home, is anyone home, how many
    # people are home, is <person> home. A bare "home" anywhere in a
    # sentence ("welcome home", "Home Assistant") is not a presence request.
    (rf"^(?:so\s+|and\s+)?who(?:'s|s|\s+is|\s+are)\s+{_NOW}{_HOME}",   "who_home"),
    (rf"^(?:is\s+|are\s+)?(?:there\s+)?(?:anyone|anybody|someone|somebody|everyone|everybody)"
     rf"\s+{_NOW}{_HOME}",                                "who_home"),
    (rf"^how\s+many\s+(?:people|persons|of\s+us)\s+(?:are\s+)?{_NOW}{_HOME}", "count_home"),
    (rf"^is\s+([a-z][a-z'\- ]{{0,40}}?)\s+{_NOW}{_HOME}$",     "person_home"),
    # The "what's/what is" lead-in is REQUIRED (not optional) — a bare
    # "open"/"unlocked" occurring anywhere in a longer sentence used to
    # match this via re.search, so an explanation, complaint, or quoted
    # alert text merely containing "unlocked" ("why did you announce
    # 'X has been unlocked for 20 minutes'") false-triggered this exact
    # status-summary shortcut instead of falling through to the real
    # conversation path. Requiring the literal "what's/what is" phrase
    # immediately before the word keeps genuine status questions ("what's
    # open", "Nova, what's unlocked") working while rejecting everything
    # that merely mentions the word.
    (r"(?:what(?:'s| is)\s+)(?:open|unlocked)\b",          "what_open"),
    (r"(?:are\s+)?(?:any|which)\s+(?:doors?|windows?)\s+open", "what_open"),
    (r"(?:list|show(?:\s+me)?|tell\s+me(?:\s+which)?|name|which|what)\s+(?:all\s+)?(?:of\s+)?(?:the\s+)?lights?\s+"
     r"(?:that\s+|which\s+)?(?:are\s+)?(?:currently\s+|still\s+|now\s+)?(?:turned\s+)?on\b",
     "lights_on"),
    (r"(?:are\s+)?(?:any|which)\s+(?:lights?)\s+on",     "lights_on"),
    (r"(?:how\s+many)\s+lights?\s+(?:are\s+)?on",        "lights_on"),
    (r"(?:what(?:'s| is)\s+(?:the\s+)?)?(?:energy|power)\s+(?:usage|consumption)", "energy"),
    (r"(?:what(?:'s| is)\s+(?:the\s+)?)?weather",        "weather"),
    (r"(?:what(?:'s| is)\s+(?:it\s+)?)?(?:like\s+)?outside", "weather"),
    (r"(?:how\s+(?:warm|cold|hot))\s+is\s+it",           "weather"),
    (r"(?:what\s+)?(?:devices?|entities?)\s+(?:are\s+)?(?:in|at)\s+(?:the\s+)?(.+)", "area_devices"),
]


# "Do not change anything", "don't turn anything off", "without changing
# it": the person asked for information only.
_READ_ONLY_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|without)\s+(?:change|changing|touch|touching|turn|turning|"
    r"switch|switching|adjust|adjusting|alter|altering)\b"
    r"|\b(?:read[\s-]?only|just\s+(?:list|tell|show))\b")


# ── Complexity scoring ──────────────────────────────────────────────────────

def _contains_word(text: str, phrase: str) -> bool:
    """Word-boundary-aware containment — unlike a bare `phrase in text`
    substring check, this does not fire on "lock" inside "unlocked" or
    "Thermo Lock" (the mechanism that let a "why did you announce ...
    unlocked ..." complaint dodge LLM escalation: "lock" matching as a
    plain substring artificially lowered its complexity score below the
    local-handling threshold). Works for multi-word phrases too, since
    \\b only needs non-word characters (or start/end of string) on each
    side of the whole phrase, not of every word inside it."""
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def score_complexity(text: str) -> int:
    """Score 0-100. Higher = needs LLM. <70 handled locally."""
    t = text.lower()
    score = 20
    # Strong LLM signals
    for w in ("explain", "why", "how does", "what do you think",
              "write", "compose", "draft", "create a", "help me",
              "analyze", "compare", "recommend", "suggest",
              "tell me about", "what happened", "story",
              "code", "script", "program", "debug", "fix this",
              "plan", "schedule", "strategy", "brainstorm",
              "summarize", "translate", "calculate"):
        if _contains_word(t, w):
            score += 40
            break
    if len(text.split()) > 20:
        score += 15
    if "?" in text and len(text.split()) > 10:
        score += 10
    for w in ("because", "however", "although"):
        if _contains_word(t, w):
            score += 10
            break
    # Simple signals (reduce)
    for w in ("turn on", "turn off", "lock", "unlock", "open", "close",
              "dim", "brighten", "set temp", "volume", "pause", "play"):
        if _contains_word(t, w):
            score -= 20
            break
    for w in ("what time", "who's home", "how many lights",
              "what's open", "temperature", "status",
              "good morning", "good night", "hello", "thanks"):
        if _contains_word(t, w):
            score -= 15
            break
    return max(0, min(100, score))


# ── Entity resolution ────────────────────────────────────────────────────────

def _fuzzy_score(a: str, b: str) -> float:
    """
    Simple fuzzy similarity score (0-100) without external deps.
    Combines character overlap, word overlap, and edit distance approximation.
    """
    if a == b:
        return 100.0
    if not a or not b:
        return 0.0

    # Character bigram overlap (Dice coefficient)
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1)) if len(s) > 1 else {s}

    bg_a, bg_b = bigrams(a), bigrams(b)
    if bg_a and bg_b:
        overlap = len(bg_a & bg_b)
        dice = (2.0 * overlap) / (len(bg_a) + len(bg_b)) * 100
    else:
        dice = 0.0

    # Word overlap
    words_a, words_b = set(a.split()), set(b.split())
    if words_a and words_b:
        common = len(words_a & words_b)
        word_score = common / max(len(words_a), len(words_b)) * 100
    else:
        word_score = 0.0

    # Containment bonus
    contain = 0.0
    if a in b:
        contain = len(a) / len(b) * 80
    elif b in a:
        contain = len(b) / len(a) * 80

    return max(dice, word_score, contain)


# Common STT mishearing patterns: what STT outputs → what user likely meant
_STT_CORRECTIONS = {
    "lamp": "light", "lamps": "lights",
    "lite": "light", "lites": "lights",
    "nite": "night", "nitestand": "nightstand",
    "night stand": "nightstand",
    "bed side": "bedside", "bed lamp": "bedside light",
    "chase": "chase", "chaise": "chase",
    "tv": "television", "teevee": "tv",
    "a.c.": "ac", "air con": "air conditioner",
    "thermos": "thermostat", "thermo": "thermostat",
    "bed room": "bedroom", "living room": "living_room",
    "bath room": "bathroom", "dining room": "dining_room",
    "down stairs": "downstairs", "up stairs": "upstairs",
    "front porch": "front_porch", "back yard": "backyard",
}


class _AmbiguousEntity:
    """Non-iterable sentinel returned by _find_entity() when two or more
    entities are plausible matches, instead of a real (entity_id,
    friendly_name) tuple. Every caller must check
    `isinstance(result, _AmbiguousEntity)` before unpacking — an `or`-chained
    fallback (see try_local's first call site) correctly short-circuits on
    it since it's truthy, but a bare `if not resolved:` guard does NOT catch
    it, so skipping the isinstance check would crash on unpack (a safe
    failure mode, never a silently wrong entity) rather than produce a
    proper clarification. `candidates` is always deduplicated by entity_id
    and always has at least 2 entries."""
    def __init__(self, candidates: list[dict]):
        seen: dict = {}
        for c in candidates:
            seen.setdefault(c["entity_id"], c)
        self.candidates = list(seen.values())


def _build_local_clarification(candidates: list[dict], addr: str) -> str:
    """Fixed, deterministic clarification — no LLM in this file at all, so
    this is inherently deterministic. If two or more candidates share a
    friendly_name, the entity_id is appended so the question never asks
    'did you mean Kitchen Light or Kitchen Light?'."""
    names = [c.get("friendly_name") or c["entity_id"] for c in candidates]
    counts: dict = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    labels = [f"{n} ({c['entity_id']})" if counts[n] > 1 else n
              for c, n in zip(candidates, names)]
    if len(labels) >= 2:
        return f"I found more than one match{addr} — did you mean {labels[0]} or {labels[1]}?"
    return f"I found more than one possible match{addr} — could you be more specific?"


def _find_entity(hass, name_fragment, domain_hint=None):
    """
    Fuzzy-match a name fragment against HA entities. v5.7.08.

    Matching tiers:
      1. Learned aliases (from agent's remember tool) — exact key, always
         unique by construction.
      2. Exact friendly_name match — ALL exact matches are collected before
         deciding; ambiguous if more than one distinct entity matches.
      3. Substring: fragment appears inside friendly_name
      4. Word overlap: all words in fragment appear in friendly_name
      5. Fuzzy similarity scoring (handles phonetic/STT errors)
      6. Entity_id substring match
      6b. STT correction → retry with corrected form (continues updating the
          SAME per-entity best-score tracking as tiers 3-6, not a fresh pass)
      7. Area-based fallback — ALL eligible entities in the first matching
         area are collected; ambiguous if more than one.

    Tiers 3-6b share one dict keyed by entity_id, holding each entity's
    HIGHEST score seen across every pass (original fragment AND
    STT-corrected fragment) — this is what guarantees the same entity can
    never occupy both the top and runner-up slot after the STT retry pass
    re-scores it (a plain best/runner-up pair could let that happen; a
    dict keyed by entity_id structurally cannot).

    Returns (entity_id, friendly_name), an _AmbiguousEntity, or None.
    """
    fragment = name_fragment.lower().strip()

    # Strip common suffixes
    for suffix in ("light", "lights", "lamp", "lamps", "switch", "switches",
                   "fan", "fans", "lock", "locks", "door", "doors",
                   "cover", "covers", "thermostat", "sensor",
                   "please", "now", "for me", "nova"):
        fragment = re.sub(rf"\s+{suffix}$", "", fragment)
    fragment = fragment.strip()
    if not fragment:
        return None

    # ── Tier 1: Check learned aliases ───────────────────────────────
    # An exact key maps to exactly one entity — always unique.
    try:
        import json as _json, os as _os
        learn_file = "/config/.nova_learned.json"
        if _os.path.exists(learn_file):
            with open(learn_file) as f:
                learned = _json.load(f)
            aliases = learned.get("alias", {})
            if fragment in aliases:
                resolved_id = aliases[fragment]
                state = hass.states.get(resolved_id)
                if state:
                    _LOGGER.info("Entity resolve: alias '%s' → %s", fragment, resolved_id)
                    return (resolved_id, state.attributes.get("friendly_name", resolved_id))
    except Exception:
        pass

    domains = [domain_hint] if domain_hint else [
        "light", "switch", "lock", "cover", "climate",
        "fan", "media_player", "sensor", "binary_sensor"]

    frag_words = set(fragment.split())
    exact_matches: dict = {}     # entity_id -> {"entity_id","friendly_name"}
    candidate_scores: dict = {}  # entity_id -> {"entity_id","friendly_name","score"} — highest score seen

    def _consider(eid, fname_display, score):
        cur = candidate_scores.get(eid)
        if cur is None or score > cur["score"]:
            candidate_scores[eid] = {
                "entity_id": eid, "friendly_name": fname_display, "score": score,
            }

    for domain in domains:
        for state in hass.states.async_all(domain):
            eid = state.entity_id
            fname = (state.attributes.get("friendly_name") or "").lower()
            fname_display = state.attributes.get("friendly_name", eid)
            fname_words = set(fname.split())

            # Tier 2: exact — collected, not returned immediately.
            if fname == fragment:
                exact_matches.setdefault(eid, {"entity_id": eid, "friendly_name": fname_display})
                continue

            # Tier 3: substring
            if fragment in fname:
                _consider(eid, fname_display, len(fragment) / max(len(fname), 1) * 100)
                continue

            # Tier 4: word overlap
            if frag_words and frag_words.issubset(fname_words):
                _consider(eid, fname_display, len(frag_words) / max(len(fname_words), 1) * 95)
                continue

            # Tier 5: fuzzy similarity
            fuzz = _fuzzy_score(fragment, fname)
            if fuzz > 55:
                _consider(eid, fname_display, fuzz)
                continue

            # Tier 6: entity_id substring
            frag_u = fragment.replace(" ", "_")
            if frag_u in eid:
                _consider(eid, fname_display, len(frag_u) / max(len(eid), 1) * 80)

    if exact_matches:
        if len(exact_matches) > 1:
            _LOGGER.info("Entity resolve: '%s' ambiguous — %d exact matches",
                         fragment, len(exact_matches))
            return _AmbiguousEntity(list(exact_matches.values()))
        only = next(iter(exact_matches.values()))
        _LOGGER.info("Entity resolve: exact '%s' → %s", fragment, only["entity_id"])
        return (only["entity_id"], only["friendly_name"])

    # ── Tier 6b: STT correction retry ────────────────────────────────
    # Continues updating the SAME candidate_scores dict (not a fresh
    # best/runner-up pair) — an entity re-scored here just has its existing
    # entry's score raised if the correction scores higher, it never becomes
    # a second, distinct candidate for itself.
    top_score_so_far = max((c["score"] for c in candidate_scores.values()), default=0)
    if not candidate_scores or top_score_so_far < 50:
        corrected = fragment
        for wrong, right in _STT_CORRECTIONS.items():
            if wrong in corrected:
                corrected = corrected.replace(wrong, right)
        if corrected != fragment:
            _LOGGER.info("Entity resolve: STT correction '%s' → '%s'", fragment, corrected)
            for domain in domains:
                for state in hass.states.async_all(domain):
                    eid = state.entity_id
                    fname = (state.attributes.get("friendly_name") or "").lower()
                    fname_display = state.attributes.get("friendly_name", eid)
                    if corrected in fname:
                        _consider(eid, fname_display, len(corrected) / max(len(fname), 1) * 90)
                    fuzz = _fuzzy_score(corrected, fname)
                    if fuzz > 55:
                        _consider(eid, fname_display, fuzz)

    # ── Tier 7: area-based fallback ────────────────────────────────
    # ALL eligible entities in the first matching area are collected before
    # deciding, rather than returning the first registry entry found.
    if not candidate_scores and domain_hint:
        try:
            from homeassistant.helpers import (
                area_registry as areg, entity_registry as er, device_registry as dr)
            area_reg = areg.async_get(hass)
            ent_reg = er.async_get(hass)
            dev_reg = dr.async_get(hass)
            for area in area_reg.async_list_areas():
                if fragment in area.name.lower():
                    eligible: dict = {}
                    for entry in ent_reg.entities.values():
                        if entry.domain != domain_hint:
                            continue
                        in_area = entry.area_id == area.id
                        if not in_area and entry.device_id:
                            device = dev_reg.async_get(entry.device_id)
                            in_area = device and device.area_id == area.id
                        if in_area:
                            state = hass.states.get(entry.entity_id)
                            if state:
                                eligible.setdefault(entry.entity_id, {
                                    "entity_id": entry.entity_id,
                                    "friendly_name": state.attributes.get(
                                        "friendly_name", entry.entity_id),
                                })
                    if len(eligible) > 1:
                        _LOGGER.info("Entity resolve: '%s' ambiguous — %d entities in area",
                                     fragment, len(eligible))
                        return _AmbiguousEntity(list(eligible.values()))
                    if len(eligible) == 1:
                        only = next(iter(eligible.values()))
                        return (only["entity_id"], only["friendly_name"])
                    # zero eligible in this area — keep checking other areas
        except Exception:
            pass

    # ── Final decision ──────────────────────────────────────────────
    if candidate_scores:
        ranked = sorted(candidate_scores.values(), key=lambda c: (-c["score"], c["entity_id"]))
        top = ranked[0]
        if top["score"] > 25:
            if len(ranked) > 1:
                second = ranked[1]
                if second["score"] > 25 and second["score"] / top["score"] >= 0.75:
                    _LOGGER.info("Entity resolve: '%s' ambiguous — top=%.0f second=%.0f",
                                 fragment, top["score"], second["score"])
                    return _AmbiguousEntity([top, second])
            _LOGGER.info("Entity resolve: '%s' → %s (score=%.0f)",
                         fragment, top["entity_id"], top["score"])
            return (top["entity_id"], top["friendly_name"])

    # A failed resolution is normal, expected control flow: the phrase looked
    # vaguely command-like but matched no device, so the caller falls through
    # to the LLM (which has fuzzy search + aliases). DEBUG, not WARNING — this
    # is not an error condition and should not surface in the user's log.
    _LOGGER.debug("Entity resolve unmatched: '%s' (domain=%s)", fragment, domain_hint)
    return None


def _find_entities_in_area(hass, area_name, domain):
    results = []
    try:
        from homeassistant.helpers import (
            area_registry as areg, entity_registry as er, device_registry as dr)
        try:
            from .entity_filter import is_excluded
        except Exception:
            is_excluded = lambda _h, _e: False
        area_reg = areg.async_get(hass)
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        target = None
        for area in area_reg.async_list_areas():
            if area_name.lower() in area.name.lower():
                target = area
                break
        if not target:
            return []
        for entry in ent_reg.entities.values():
            if entry.domain != domain:
                continue
            in_area = entry.area_id == target.id
            if not in_area and entry.device_id:
                device = dev_reg.async_get(entry.device_id)
                in_area = device and device.area_id == target.id
            if in_area:
                # Excluded entities don't take part in area/group commands
                # ("turn on the living-room lights"); a by-name request still
                # resolves them through a different path.
                if is_excluded(hass, entry.entity_id):
                    continue
                state = hass.states.get(entry.entity_id)
                if state:
                    results.append((entry.entity_id,
                                    state.attributes.get("friendly_name", entry.entity_id)))
    except Exception:
        pass
    return results


def _find_scene_or_script(hass, name_fragment):
    fragment = name_fragment.lower().strip()
    for suffix in ("scene", "script", "routine", "mode"):
        fragment = re.sub(rf"\s+{suffix}$", "", fragment)
    fragment = fragment.strip()
    for domain, dtype in [("scene", "scene"), ("script", "script")]:
        for state in hass.states.async_all(domain):
            fname = (state.attributes.get("friendly_name") or "").lower()
            if fname == fragment or fragment in fname:
                return (state.entity_id,
                        state.attributes.get("friendly_name", state.entity_id), dtype)
            if fragment.replace(" ", "_") in state.entity_id:
                return (state.entity_id,
                        state.attributes.get("friendly_name", state.entity_id), dtype)
    return None


# ── Action execution ────────────────────────────────────────────────────────

def _service_for(action, entity_id):
    """Map a fast-path action to the (domain, service) it would call, or None if
    it isn't a direct actuation. Mirrors _execute_action so the confirmation
    pre-check matches exactly what would run."""
    dom = entity_id.split(".")[0] if entity_id else ""
    if action in ("turn_on", "turn_off", "toggle"):
        return (dom, action)
    fixed = {
        "lock": ("lock", "lock"), "unlock": ("lock", "unlock"),
        "open": ("cover", "open_cover"), "close": ("cover", "close_cover"),
        "dim": ("light", "turn_on"), "brighten": ("light", "turn_on"),
        "media_pause": ("media_player", "media_pause"),
        "media_play": ("media_player", "media_play"),
        "media_next": ("media_player", "media_next_track"),
        "volume_up": ("media_player", "volume_up"),
        "volume_down": ("media_player", "volume_down"),
        "mute": ("media_player", "volume_mute"),
        "unmute": ("media_player", "volume_mute"),
        "set_temp": ("climate", "set_temperature"),
        "set_temp_named": ("climate", "set_temperature"),
        "volume_set": ("media_player", "volume_set"),
    }
    return fixed.get(action)


def _policy_requires_confirmation(hass, domain, service, entity_id="",
                                  device_id=None) -> bool:
    """policy.requires_confirmation for the fast path, passing the request's
    device so the voice-satellite rule (unlock/open needs a phone tap) applies
    here exactly as it does on the agent path. device_id is only passed when
    there is one, so typed/chat calls are unchanged.

    If the policy check itself fails, anything above low risk is deferred to
    the agent (fail closed): the agent runs the real confirmation gate, so
    deferring can never actuate a protected action unconfirmed. Low-risk
    convenience actions keep running locally."""
    try:
        from . import policy
    except Exception:
        return True
    try:
        if device_id:
            return bool(policy.requires_confirmation(
                hass, domain, service, entity_id, device_id=device_id))
        return bool(policy.requires_confirmation(hass, domain, service, entity_id))
    except Exception as exc:
        _LOGGER.warning("Local: confirmation check failed for %s.%s (%s)",
                        domain, service, exc)
        try:
            risk, _ = policy.classify(domain, service, entity_id)
        except Exception:
            return True
        return risk != "low"


def _needs_confirmation(hass, action, entity_id="", device_id=None) -> bool:
    """Whether this fast-path action would require confirmation. Protected
    actions (unlock, open a garage/cover, disarm, …) are deferred to the agent
    rather than actuated here, so the same authorization gate that guards the
    agent's tools also guards the fast-path — the fast-path never becomes a
    second, unguarded way to unlock a door. `device_id` is the requesting
    device (a voice satellite for spoken requests)."""
    svc = _service_for(action, entity_id)
    if not svc:
        return False
    return _policy_requires_confirmation(hass, svc[0], svc[1], entity_id, device_id)


def _needs_confirmation_domain(hass, domain, service, entity_id="", device_id=None) -> bool:
    """Like _needs_confirmation, but for callers that already know the
    (domain, service) pair rather than a fast-path action name — scene/script/
    automation activation doesn't go through _service_for's action mapping, so
    it needs its own entry point onto the same policy check."""
    return _policy_requires_confirmation(hass, domain, service, entity_id, device_id)


async def _execute_action(hass, action, entity_id, args):
    try:
        domain = entity_id.split(".")[0]
        svc_map = {
            "turn_on": (domain, "turn_on"), "turn_off": (domain, "turn_off"),
            "toggle": (domain, "toggle"), "lock": ("lock", "lock"),
            "unlock": ("lock", "unlock"), "open": ("cover", "open_cover"),
            "close": ("cover", "close_cover"), "dim": ("light", "turn_on"),
            "brighten": ("light", "turn_on"),
            "media_pause": ("media_player", "media_pause"),
            "media_play": ("media_player", "media_play"),
            "media_next": ("media_player", "media_next_track"),
            "volume_up": ("media_player", "volume_up"),
            "volume_down": ("media_player", "volume_down"),
            "mute": ("media_player", "volume_mute"),
            "unmute": ("media_player", "volume_mute"),
        }

        if action in svc_map:
            svc_domain, svc_name = svc_map[action]
            svc_data = {"entity_id": entity_id}
            if action == "dim" and "brightness_pct" in args:
                svc_data["brightness_pct"] = args["brightness_pct"]
            elif action == "brighten":
                svc_data["brightness_pct"] = 100
            elif action == "mute":
                svc_data["is_volume_muted"] = True
            elif action == "unmute":
                svc_data["is_volume_muted"] = False
            await hass.services.async_call(svc_domain, svc_name, svc_data, blocking=True)
            return True
        elif action in ("set_temp", "set_temp_named"):
            temp = args.get("temperature")
            if temp:
                await hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": entity_id, "temperature": float(temp)}, blocking=True)
                return True
        elif action == "volume_set":
            level = args.get("volume_level", 50)
            await hass.services.async_call(
                "media_player", "volume_set",
                {"entity_id": entity_id, "volume_level": level / 100.0}, blocking=True)
            return True
        return False
    except Exception as exc:
        _LOGGER.warning("Local action failed for %s: %s", entity_id, exc)
        return False


_LOCK_COVER_SERVICE = {
    "lock":   ("lock", "lock"),
    "unlock": ("lock", "unlock"),
    "open":   ("cover", "open_cover"),
    "close":  ("cover", "close_cover"),
}


async def _execute_action_verified(hass, action, entity_id, args, fname):
    """Single-entity wrapper around _execute_action that reports Nova's
    Phase 3 status vocabulary ("verified"/"accepted"/"unverified"/"error"),
    using the SAME entity_verify helpers and the SAME background
    _verify_control agent.py's own tool-calling path uses, so the two paths
    never disagree about what "verified" means. Bulk/multi-entity call sites
    (the "turn off all lights" and "all switches in area" shortcuts) deliberately
    do NOT go through this wrapper -- polling each entity synchronously would
    multiply latency by the number of entities, so they keep the existing
    fire-and-forget behavior unchanged.
    """
    domain = entity_id.split(".")[0]
    pre_state = hass.states.get(entity_id)
    pre = pre_state.state if pre_state else None

    # Action Audit Log (top-level boundary: this is the sole caller of
    # _execute_action_verified, and it's always a genuine top-level voice
    # action — _needs_confirmation() already sends anything protected to
    # the agent instead, so this path never calls confirm_gate).
    from . import action_log
    request_id = action_log.new_request_id()
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "control_device", "voice",
            domain=domain, entity_id=entity_id,
            requested_state=str(args.get("brightness_pct") or args.get("temperature")
                                or args.get("volume_level") or action),
        )
    )

    ok = await _execute_action(hass, action, entity_id, args)
    if not ok:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed", reason_code="service_call_failed")
        )
        return "error"
    await hass.async_add_executor_job(
        lambda: action_log.set_execution(action_id, "accepted")
    )

    from . import entity_verify

    if action in ("turn_on", "turn_off"):
        if domain in entity_verify.FAST_VERIFY_DOMAINS:
            expected = "on" if action == "turn_on" else "off"
            verified = await entity_verify.wait_until(
                lambda: entity_verify.check_state_once(hass, entity_id, expected))
            if verified:
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(action_id, "verified")
                )
                return "verified"
        from .agent import _verify_control
        hass.async_create_task(
            _verify_control(hass, entity_id, action, domain, action,
                             {"entity_id": entity_id}, source="local_engine",
                             action_id=action_id))
        return "accepted" if domain not in entity_verify.FAST_VERIFY_DOMAINS else "unverified"

    if action == "toggle":
        if domain not in entity_verify.FAST_VERIFY_DOMAINS:
            return "accepted"   # never sent to _verify_control (see docstring)
        expected = {"on": "off", "off": "on"}.get(pre)
        if expected is None:
            return "accepted"
        verified = await entity_verify.wait_until(
            lambda: entity_verify.check_state_once(hass, entity_id, expected))
        if verified:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(action_id, "verified")
            )
            return "verified"
        await entity_verify.record_unverified(
            hass, entity_id, "toggle", source="local_engine",
            detail=", not retried automatically because repeating toggle "
                   "could reverse a delayed successful action")
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "unverified", reason_code="not_retried_toggle_could_reverse")
        )
        return "unverified"

    if action in ("dim", "brighten"):
        requested_pct = args.get("brightness_pct", 100 if action == "brighten" else 50)
        verified = await entity_verify.wait_until(
            lambda: entity_verify.check_brightness_once(hass, entity_id, requested_pct))
        if verified:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(action_id, "verified")
            )
            return "verified"
        await entity_verify.record_unverified(
            hass, entity_id, action, source="local_engine",
            detail=(f", requested {requested_pct}%, not retried automatically "
                    "because the background verifier cannot validate the "
                    "requested brightness level"))
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "unverified", reason_code="brightness_not_confirmed")
        )
        return "unverified"

    if action in _LOCK_COVER_SERVICE:
        v_dom, v_svc = _LOCK_COVER_SERVICE[action]
        from .agent import _verify_control
        hass.async_create_task(
            _verify_control(hass, entity_id, action, v_dom, v_svc,
                             {"entity_id": entity_id}, source="local_engine",
                             action_id=action_id))
        return "accepted"

    return "accepted"


# ── Response generation ──────────────────────────────────────────────────────

def _resp(action, fname, success, args=None, h="sir", status=None):
    # h may be "" once nobody specific is home to address (see honorific.py)
    # — addr collapses the trailing ", {h}" to nothing rather than a
    # dangling comma.
    addr = f", {h}" if h else ""
    if not success:
        # Nova reports failure calmly and precisely, no hand-wringing.
        return (f"I wasn't able to {action.replace('_', ' ')} {fname}{addr} — "
                f"there may be a connectivity issue.")

    # Phase 3: the command was sent and didn't error, but the bounded
    # synchronous check (light/switch/fan turn_on/turn_off/toggle, dim,
    # brighten) never confirmed it landed. Report honestly instead of
    # claiming it's done -- toggle/dim/brighten are never auto-retried
    # (see entity_verify.record_unverified), turn_on/turn_off get a
    # background retry via the existing _verify_control.
    if status == "unverified":
        bp = (args or {}).get("brightness_pct", 100 if action == "brighten" else "?")
        if action in ("dim", "brighten"):
            return (f"I sent the command to set {fname} to {bp}%{addr}, but I "
                     f"can't confirm the brightness reached that level yet.")
        verb = action.replace("_", " ")
        return (f"I sent the command to {verb} {fname}{addr}, but I can't "
                f"confirm it worked yet.")

    # status == "accepted": the command was sent and didn't error, but
    # nothing here confirms it landed -- lock/unlock/open/close rely on the
    # existing background _verify_control (unchanged); toggle on a non-fast
    # domain and every climate/media action never get a synchronous check at
    # all. Neutral, in-progress wording only. Deliberately does NOT use the
    # completion-style lead-ins below (_pick(["Secured", ... "Consider it
    # done", ...])) -- those read as confirmed even paired with "now"
    # phrasing, which is the exact overclaim this branch exists to avoid.
    if status == "accepted":
        temp = (args or {}).get("temperature", "?")
        vol = (args or {}).get("volume_level", "?")
        accepted_r = {
            "turn_on":        f"I've sent the command to turn on {fname}{addr}.",
            "turn_off":       f"I've sent the command to turn off {fname}{addr}.",
            "toggle":         f"I've sent the toggle command to {fname}{addr}.",
            "lock":           f"Locking {fname} now{addr}.",
            "unlock":         f"Unlocking {fname} now{addr}.",
            "open":           f"Opening {fname} now{addr}.",
            "close":          f"Closing {fname} now{addr}.",
            "set_temp":       f"I've sent the command to set the temperature to {temp}°{addr}.",
            "set_temp_named": f"I've sent the command to set {fname} to {temp}°{addr}.",
            "media_pause":    f"I've sent the pause command{addr}.",
            "media_play":     f"I've sent the play command{addr}.",
            "media_next":     f"I've sent the next-track command{addr}.",
            "volume_up":      f"I've sent the volume-up command{addr}.",
            "volume_down":    f"I've sent the volume-down command{addr}.",
            "volume_set":     f"I've sent the command to set the volume to {vol}%{addr}.",
            "mute":           f"I've sent the mute command{addr}.",
            "unmute":         f"I've sent the unmute command{addr}.",
        }
        return accepted_r.get(
            action, f"I've sent the {action.replace('_', ' ')} command to {fname}{addr}.")

    # status == "verified": the state was actually confirmed, so completion
    # wording is accurate here -- the only actions that ever reach "verified"
    # are turn_on/turn_off/toggle on a fast domain, and dim/brighten.
    # Understated lead-ins in Nova's own voice. Varied so confirmations never sound
    # canned. Each is something Nova would actually say.
    ack = _pick(["Done", "Right away", "As you wish", "Consider it done", "At once"])

    bp = (args or {}).get('brightness_pct', '?')

    r = {
        "turn_on":  f"{ack}{addr}. {fname} is on.",
        "turn_off": f"{ack}{addr}. {fname} is off.",
        "toggle":   f"{ack}{addr}. {fname} toggled.",
        "dim":      f"{ack}{addr}. {fname} at {bp}%.",
        "brighten": f"{ack}{addr}. {fname} at full brightness.",
    }
    return r.get(action, f"{ack}{addr}. {action} applied to {fname}.")


def _query_resp(hass, action, entity_id, fname, h="sir"):
    addr = f", {h}" if h else ""
    if action == "query_time":
        from homeassistant.util import dt as dt_util
        return f"It's currently {dt_util.now().strftime('%I:%M %p')}{addr}."
    if action == "query_date":
        from homeassistant.util import dt as dt_util
        return f"Today is {dt_util.now().strftime('%A, %B %d, %Y')}{addr}."
    if action == "greeting":
        from homeassistant.util import dt as dt_util
        hour = dt_util.now().hour
        g = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
        tail = _pick([f"{g}{addr}.",
                      f"{g}{addr}. What can I do for you?",
                      f"{g}{addr}. At your disposal."])
        return tail
    if action == "thanks":
        return _pick([f"Of course{addr}.",
                      f"My pleasure{addr}.",
                      f"Anytime{addr}."])
    if action == "query_temp":
        state = hass.states.get(entity_id)
        if state:
            unit = state.attributes.get("unit_of_measurement", "°")
            return f"The temperature in {fname} is currently {state.state}{unit}{addr}."
        return f"I'm unable to read the temperature for {fname} at the moment{addr}."
    if action == "query_state":
        state = hass.states.get(entity_id)
        if state:
            return f"{fname} is currently {state.state}{addr}."
        return f"I'm unable to determine the status of {fname} at the moment{addr}."
    if action == "status":
        return _home_status(hass, h)
    return ""


def _home_status(hass, h="sir"):
    addr = f", {h}" if h else ""
    from .cognitive_core import _lockdown_exempt_locks
    exempt = _lockdown_exempt_locks()
    lights_on = sum(1 for s in hass.states.async_all("light") if s.state == "on")
    locks_ul = sum(1 for s in hass.states.async_all("lock")
                   if s.state == "unlocked" and s.entity_id not in exempt)
    doors_open = sum(1 for s in hass.states.async_all("binary_sensor")
                     if s.attributes.get("device_class") == "door" and s.state == "on")
    people = sum(1 for s in hass.states.async_all("person") if s.state == "home")
    parts = [f"All systems nominal{addr}."]
    parts.append(f"{lights_on} light{'s' if lights_on != 1 else ''} on.")
    parts.append("All locks secured." if not locks_ul else
                 f"{locks_ul} lock{'s' if locks_ul != 1 else ''} unlocked.")
    if doors_open:
        parts.append(f"{doors_open} door{'s' if doors_open != 1 else ''} open.")
    parts.append(f"{people} person{'s' if people != 1 else ''} home.")
    return " ".join(parts)


# ── Contextual queries ──────────────────────────────────────────────────────

def _platform_status(hass, addr: str) -> str:
    """A bounded, truthful answer about Home Assistant itself, from what
    this running instance can observe: its run state, version and how many
    entities it serves. Nova runs inside Home Assistant, so answering at all
    means the platform is up; nothing here claims more than that."""
    try:
        from homeassistant.const import __version__ as ha_version
        ver = f" {ha_version}" if re.match(r"^\d", str(ha_version)) else ""
    except Exception:
        ver = ""
    try:
        count = hass.states.async_entity_ids_count()
    except Exception:
        try:
            count = len(hass.states.async_all())
        except Exception:
            count = None
    state = getattr(getattr(hass, "state", None), "value", getattr(hass, "state", None))
    state = str(state) if state is not None else "running"
    if state != "running":
        return (f"Home Assistant{ver} is {state} right now{addr}, so some devices may not "
                f"respond until it finishes.")
    if not count:
        return f"Home Assistant{ver} is running{addr}, but I can't see any entities right now."
    noun = "entity" if count == 1 else "entities"
    return (f"Home Assistant{ver} is running and responding{addr}. I'm connected to it "
            f"and can see {count} {noun}.")


def _ctx_query(hass, qtype, h="sir", area_match=""):
    addr = f", {h}" if h else ""
    if qtype == "platform_status":
        return _platform_status(hass, addr)

    if qtype in ("count_home", "person_home"):
        people = list(hass.states.async_all("person"))
        if qtype == "count_home":
            home = sorted(s.attributes.get("friendly_name", s.entity_id)
                          for s in people if s.state == "home")
            if not home:
                return f"No one appears to be home at the moment{addr}."
            if len(home) == 1:
                return f"One person is home{addr}: {home[0]}."
            return f"{len(home)} people are home{addr}: {', '.join(home[:-1])} and {home[-1]}."
        wanted = " ".join(area_match.replace("_", " ").split())
        for s in sorted(people, key=lambda p: p.entity_id):
            name = str(s.attributes.get("friendly_name") or "")
            if wanted and wanted in (name.lower(), s.entity_id.split(".", 1)[1].replace("_", " ")):
                shown = name or s.entity_id
                if s.state == "home":
                    return f"{shown} is home{addr}."
                if s.state == "not_home":
                    return f"{shown} is away{addr}."
                if s.state in ("unknown", "unavailable"):
                    return f"I can't tell where {shown} is right now{addr}."
                return f"{shown} is at {s.state}{addr}."
        return None     # not a known person: let the agent handle it

    if qtype == "who_home":
        ppl = [s.attributes.get("friendly_name", s.entity_id)
               for s in hass.states.async_all("person") if s.state == "home"]
        if not ppl:
            return f"No one appears to be home at the moment{addr}."
        if len(ppl) == 1:
            return f"{ppl[0]} is currently home{addr}."
        return f"{', '.join(ppl[:-1])} and {ppl[-1]} are currently home{addr}."

    if qtype == "what_open":
        items = []
        for s in hass.states.async_all("binary_sensor"):
            dc = s.attributes.get("device_class", "")
            if dc in ("door", "window", "garage_door") and s.state == "on":
                items.append(s.attributes.get("friendly_name", s.entity_id))
        for s in hass.states.async_all("cover"):
            if s.state == "open":
                items.append(s.attributes.get("friendly_name", s.entity_id))
        from .cognitive_core import _lockdown_exempt_locks
        exempt = _lockdown_exempt_locks()
        for s in hass.states.async_all("lock"):
            if s.state == "unlocked" and s.entity_id not in exempt:
                items.append(s.attributes.get("friendly_name", s.entity_id) + " (unlocked)")
        if not items:
            return f"Everything is closed and secured{addr}."
        return f"Currently open or unlocked: {', '.join(items)}."

    if qtype == "lights_on":
        # Read-only: names come from the presentation helper (friendly name,
        # area added when two lights share a name); no service is called.
        from .agent_runtime.presentation import display_names
        on_ids = [s.entity_id for s in hass.states.async_all("light") if s.state == "on"]
        names = display_names(hass, on_ids)
        on = [names.get(eid, eid) for eid in on_ids]
        if not on:
            return f"All lights are off{addr}."
        c = len(on)
        listing = ", ".join(on[:5])
        return f"{c} light{'s' if c != 1 else ''} on: {listing}{'...' if c > 5 else ''}."

    if qtype == "energy":
        for s in hass.states.async_all("sensor"):
            if s.attributes.get("device_class") == "power" and "total" in s.entity_id.lower():
                unit = s.attributes.get("unit_of_measurement", "W")
                return f"Current power consumption is {s.state} {unit}{addr}."
        return f"I don't have a total power sensor configured{addr}."

    if qtype == "weather":
        for s in hass.states.async_all("weather"):
            temp = s.attributes.get("temperature", "—")
            humidity = s.attributes.get("humidity", "—")
            condition = s.state.replace("_", " ")
            return f"Currently {condition} outside{addr}. Temperature is {temp}° with {humidity}% humidity."
        for s in hass.states.async_all("sensor"):
            if ("outdoor" in s.entity_id.lower() or "outside" in s.entity_id.lower()):
                if s.attributes.get("device_class") == "temperature":
                    unit = s.attributes.get("unit_of_measurement", "°")
                    return f"The outdoor temperature is {s.state}{unit}{addr}."
        return f"I don't have weather data available at the moment{addr}."

    if qtype == "area_devices" and area_match:
        try:
            from homeassistant.helpers import (
                area_registry as areg, entity_registry as er, device_registry as dr)
            area_reg = areg.async_get(hass)
            ent_reg = er.async_get(hass)
            dev_reg = dr.async_get(hass)
            target = None
            for area in area_reg.async_list_areas():
                if area_match.lower() in area.name.lower():
                    target = area
                    break
            if not target:
                return f"I couldn't find an area matching '{area_match}'{addr}."
            devs = []
            for entry in ent_reg.entities.values():
                in_area = entry.area_id == target.id
                if not in_area and entry.device_id:
                    device = dev_reg.async_get(entry.device_id)
                    in_area = device and device.area_id == target.id
                if in_area:
                    state = hass.states.get(entry.entity_id)
                    if state:
                        devs.append(f"{state.attributes.get('friendly_name', entry.entity_id)} ({state.state})")
            if not devs:
                return f"No devices found in {target.name}{addr}."
            listing = ", ".join(devs[:10])
            more = f" and {len(devs) - 10} more" if len(devs) > 10 else ""
            return f"{target.name} has {len(devs)} device{'s' if len(devs) != 1 else ''}: {listing}{more}."
        except Exception:
            pass
    return None


# ── Main entry point ─────────────────────────────────────────────────────────

async def try_local(hass, text, honorific="sir", force=False, device_id=None):
    """
    PRIMARY handler. Returns LocalResult if handled, None for LLM fallback.

    When force=True (offline salvage mode), the complexity gate is bypassed:
    Nova attempts to extract and execute any actionable device command or
    answerable query from the text even if it looks complex, because escalating
    to the cloud LLM is not an option. Conversational/creative requests that
    have no local handler still return None — the caller supplies an honest
    offline response.

    device_id is the requesting device (conversation.py passes the voice
    satellite's). Every confirmation pre-check below receives it, so a spoken
    unlock/open defers to the agent's guarded path (phone tap) instead of
    actuating here — including in force/offline mode, where deferring means
    the protected action does not run at all.
    """
    # honorific may be "" once nobody specific is home to address (see
    # honorific.py) — addr collapses the trailing ", {honorific}" to
    # nothing rather than a dangling comma.
    addr = f", {honorific}" if honorific else ""
    normalized = _normalize(text)
    complexity = score_complexity(text)

    # High complexity → immediate LLM (skipped in force/offline mode)
    if complexity >= 70 and not force:
        _LOGGER.debug("Local: complexity %d for '%s' — LLM", complexity, text[:60])
        return None

    # Contextual queries
    for pattern, qtype in _QUERY_PATTERNS:
        match = re.search(pattern, normalized)
        if match:
            if qtype == "repeat_last":
                # Deterministic, no LLM/network — reads only the in-memory
                # mirror kept by spoken_history.py (never SQLite directly;
                # this runs on the event loop). Always handled, even when
                # there's nothing to repeat, so it can never fall through
                # to the LLM. See spoken_history.py's module docstring for
                # why this can survive a restart without Nova needing to
                # speak something new first (hydrate() at startup).
                from . import spoken_history
                last = spoken_history.get_last()
                if last is None:
                    return LocalResult(
                        text="I don't have a recent spoken message to repeat.",
                        success=False,
                    )
                return LocalResult(text=last["text"], success=True, repeat_of_id=last["id"])
            area_m = match.group(1) if match.lastindex else ""
            resp = _ctx_query(hass, qtype, honorific, area_m)
            if resp:
                _LOGGER.info("Local contextual: %s", qtype)
                return LocalResult(text=resp, success=True)

    # A request that says not to change anything is read-only: the local
    # engine never actuates for it. Unanswered here, it goes to the agent.
    if _READ_ONLY_RE.search(normalized):
        _LOGGER.debug("Local: read-only request, no local action")
        return None

    # Bulk/multi-entity
    for pattern, action, domain, scope in _BULK_PATTERNS:
        match = re.search(pattern, normalized)
        if not match:
            continue
        area_name = match.group(1) if scope == "area" and match.lastindex else None
        if _needs_confirmation(hass, action, "", device_id):
            _LOGGER.info("Local: bulk '%s' needs confirmation — deferring to agent", action)
            return None   # protected bulk → agent skips/confirms per device
        from . import action_log
        if domain == "all":
            all_ents: list = []
            for d in ("light", "switch", "fan"):
                all_ents.extend(
                    (s.entity_id, d) for s in hass.states.async_all(d) if s.state == "on")
            request_id = action_log.new_request_id()
            row_ids = await hass.async_add_executor_job(
                lambda: action_log.start_many(
                    request_id, "bulk_control", "chat",
                    [{"key": eid, "domain": d, "service": "turn_off", "entity_id": eid}
                     for eid, d in all_ents],
                )
            )
            total = 0
            for eid, d in all_ents:
                row_id = row_ids.get(eid)
                if await _execute_action(hass, "turn_off", eid, {}):
                    total += 1
                    if row_id is not None:
                        await hass.async_add_executor_job(
                            lambda rid=row_id: action_log.set_execution(rid, "accepted")
                        )
                elif row_id is not None:
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )
            return LocalResult(text=f"Done{addr}. {total} device{'s' if total != 1 else ''} turned off.", success=True)
        entities = _find_entities_in_area(hass, area_name, domain) if area_name else [
            (s.entity_id, s.attributes.get("friendly_name", s.entity_id))
            for s in hass.states.async_all(domain)]
        if not entities:
            continue
        request_id = action_log.new_request_id()
        row_ids = await hass.async_add_executor_job(
            lambda: action_log.start_many(
                request_id, "bulk_control", "chat",
                [{"key": eid, "domain": domain, "entity_id": eid} for eid, _fn in entities],
            )
        )
        ok = 0
        for eid, fn in entities:
            row_id = row_ids.get(eid)
            if await _execute_action(hass, action, eid, {}):
                ok += 1
                if row_id is not None:
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
            elif row_id is not None:
                await hass.async_add_executor_job(
                    lambda rid=row_id: action_log.set_execution(
                        rid, "failed", reason_code="service_call_failed")
                )
        area_str = f" in {area_name}" if area_name else ""
        verb = action.replace("_", " ")
        return LocalResult(text=f"Done{addr}. {ok} {domain}{'s' if ok != 1 else ''}{area_str} {verb}.", success=ok > 0)

    # Scene/script (check before single-entity to catch "activate X")
    for pattern, action, _ in _INTENT_PATTERNS:
        if action != "scene":
            continue
        match = re.search(pattern, normalized)
        if not match:
            continue
        name_frag = match.group(1) if match.lastindex else ""
        if not name_frag:
            continue
        found = _find_scene_or_script(hass, name_frag)
        if found:
            eid, fname, dtype = found
            # A scene/script's contents are opaque here (v5.9.06 fast-path was
            # never brought under the same gate the single-entity and bulk
            # paths below already use) — it could unlock a door or disarm the
            # alarm just as easily as dim a light. Defer to the agent the same
            # way a protected single-entity action does, rather than actuating
            # it directly from the fast path.
            if _needs_confirmation_domain(hass, dtype, "turn_on", eid, device_id):
                _LOGGER.info("Local: scene/script '%s' needs confirmation — deferring to agent", eid)
                return None   # protected activation → the agent runs the confirmation gate
            from . import action_log
            _req_id = action_log.new_request_id()
            _action_id = await hass.async_add_executor_job(
                lambda: action_log.start(
                    _req_id, "scene_activation" if dtype == "scene" else "script_run", "chat",
                    domain=dtype, service="turn_on", entity_id=eid,
                )
            )
            try:
                await hass.services.async_call(dtype, "turn_on", {"entity_id": eid}, blocking=True)
                _update_ctx(entity=eid, domain=dtype)
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(_action_id, "accepted")
                )
                return LocalResult(text=f"Activating {fname} now{addr}.", success=True)
            except Exception as exc:
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(
                        _action_id, "failed", reason_code="service_call_failed")
                )
                return LocalResult(text=f"I wasn't able to activate {fname}{addr}. {exc}", success=False)

    # Goodnight shortcut
    if re.search(r"^good\s*night(?:\s+nova)?$", normalized):
        found = _find_scene_or_script(hass, "goodnight") or _find_scene_or_script(hass, "good night")
        if found:
            eid, fname, dtype = found
            if _needs_confirmation_domain(hass, dtype, "turn_on", eid, device_id):
                _LOGGER.info("Local: goodnight scene '%s' needs confirmation — deferring to agent", eid)
                return None   # protected activation → the agent runs the confirmation gate
            from . import action_log
            _req_id = action_log.new_request_id()
            _action_id = await hass.async_add_executor_job(
                lambda: action_log.start(
                    _req_id, "scene_activation", "chat",
                    domain=dtype, service="turn_on", entity_id=eid,
                )
            )
            try:
                await hass.services.async_call(dtype, "turn_on", {"entity_id": eid}, blocking=True)
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(_action_id, "accepted")
                )
                return LocalResult(text=f"Goodnight{addr}. I've triggered {fname}. Rest well.", success=True)
            except Exception:
                await hass.async_add_executor_job(
                    lambda: action_log.set_execution(
                        _action_id, "failed", reason_code="service_call_failed")
                )
                pass
        # Phase 3: don't claim "lights off"/"locks secured" before checking --
        # report what was SENT, then verify each entity in the background
        # (same _verify_control the agent path uses, so a jammed lock or a
        # light that didn't respond gets the same honest retry-then-report
        # treatment either way).
        from .agent import _verify_control
        from . import action_log
        goodnight_request_id = action_log.new_request_id()

        light_candidates = [s.entity_id for s in hass.states.async_all("light") if s.state == "on"]
        light_row_ids = await hass.async_add_executor_job(
            lambda: action_log.start_many(
                goodnight_request_id, "goodnight_sweep", "chat",
                [{"key": eid, "domain": "light", "service": "turn_off", "entity_id": eid}
                 for eid in light_candidates],
            )
        ) if light_candidates else {}
        sent_lights: list[str] = []
        for eid in light_candidates:
            row_id = light_row_ids.get(eid)
            try:
                await hass.services.async_call(
                    "light", "turn_off", {"entity_id": eid}, blocking=True)
                sent_lights.append(eid)
                if row_id is not None:
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
            except Exception:
                if row_id is not None:
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )
        for eid in sent_lights:
            hass.async_create_task(
                _verify_control(hass, eid, "turn_off", "light", "turn_off",
                                 {"entity_id": eid}, source="local_engine",
                                 action_id=light_row_ids.get(eid)))

        from .cognitive_core import _lockdown_exempt_locks
        exempt = _lockdown_exempt_locks()
        lock_candidates = [
            s.entity_id for s in hass.states.async_all("lock")
            if s.state == "unlocked" and s.entity_id not in exempt
        ]
        lock_row_ids = await hass.async_add_executor_job(
            lambda: action_log.start_many(
                goodnight_request_id, "goodnight_sweep", "chat",
                [{"key": eid, "domain": "lock", "service": "lock", "entity_id": eid}
                 for eid in lock_candidates],
            )
        ) if lock_candidates else {}
        sent_locks: list[str] = []
        for eid in lock_candidates:
            row_id = lock_row_ids.get(eid)
            try:
                await hass.services.async_call(
                    "lock", "lock", {"entity_id": eid}, blocking=True)
                sent_locks.append(eid)
                if row_id is not None:
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
            except Exception:
                if row_id is not None:
                    await hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )
        for eid in sent_locks:
            hass.async_create_task(
                _verify_control(hass, eid, "lock", "lock", "lock",
                                 {"entity_id": eid}, source="local_engine",
                                 action_id=lock_row_ids.get(eid)))

        n_lights, n_locks = len(sent_lights), len(sent_locks)
        return LocalResult(
            text=(f"Goodnight{addr}. I've sent the command to turn off "
                  f"{n_lights} light{'s' if n_lights != 1 else ''} and lock "
                  f"{n_locks} lock{'s' if n_locks != 1 else ''}. Rest well."),
            success=True)

    # Single-entity patterns
    _last_failed_name = None  # Track for end-of-loop error
    for pattern, action, domain_hint in _INTENT_PATTERNS:
        if action == "scene":
            continue
        match = re.search(pattern, normalized)
        if not match:
            continue
        _LOGGER.info("Local intent: action=%s", action)
        if action in ("query_time", "query_date", "greeting", "thanks", "status"):
            return LocalResult(text=_query_resp(hass, action, "", "", honorific), success=True)
        groups = match.groups()
        name_frag = groups[0] if groups else ""
        extra_arg = groups[1] if len(groups) > 1 else None
        if not name_frag:
            continue
        resolved = _find_entity(hass, name_frag, domain_hint) or _find_entity(hass, name_frag, None)
        if isinstance(resolved, _AmbiguousEntity):
            return LocalResult(
                text=_build_local_clarification(resolved.candidates, addr),
                success=False,
            )
        if not resolved:
            # Track failure but keep trying other patterns/domains
            _last_failed_name = name_frag
            _LOGGER.info(
                "Local: pattern matched '%s' but entity '%s' not found "
                "(domain=%s), trying next pattern...",
                action, name_frag, domain_hint,
            )
            continue
        entity_id, fname = resolved
        args = {}
        if action == "dim" and extra_arg:
            args["brightness_pct"] = int(extra_arg)
        elif action in ("set_temp", "set_temp_named") and extra_arg:
            args["temperature"] = int(extra_arg)
        elif action in ("set_temp", "set_temp_named") and groups:
            try: args["temperature"] = int(groups[0])
            except (ValueError, IndexError): pass
        elif action == "volume_set" and groups:
            try: args["volume_level"] = int(groups[0])
            except (ValueError, IndexError): pass
        if action.startswith("query_"):
            resp = _query_resp(hass, action, entity_id, fname, honorific)
            if resp:
                return LocalResult(text=resp, success=True)
            continue
        if _needs_confirmation(hass, action, entity_id, device_id):
            _LOGGER.info("Local: '%s' on %s needs confirmation — deferring to agent",
                         action, entity_id)
            return None   # protected action → the agent runs the confirmation gate
        status = await _execute_action_verified(hass, action, entity_id, args, fname)
        success = status != "error"
        _update_ctx(entity=entity_id, domain=entity_id.split(".")[0], action=action)
        return LocalResult(
            text=_resp(action, fname, success, args, honorific, status=status),
            success=success)

    # v5.7.08: If patterns matched but entity resolution failed, ALWAYS
    # fall through to the agentic LLM. The agent has search_entities which
    # uses fuzzy matching and learned aliases — much better at finding
    # devices than the local regex resolver. Never return a local error
    # for device commands.
    # v5.9.06: In force/offline mode there's no LLM to escalate to, so return
    # an honest local error naming the device we couldn't resolve.
    if _last_failed_name:
        if force:
            return LocalResult(
                text=(
                    f"I couldn't find a device matching '{_last_failed_name}'"
                    f"{addr}, and I'm offline so I can't do a deeper search "
                    f"right now. Try the exact device name."
                ),
                success=False,
            )
        _LOGGER.info(
            "Local: entity '%s' not found — escalating to agentic LLM "
            "with search_entities",
            _last_failed_name,
        )
        return None  # Fall through to agent

    # ── Appliance monitor queries ───────────────────────────────────
    appliance_match = re.search(
        r"(?:let\s+me\s+know|tell\s+me|notify\s+me|alert\s+me)"
        r".*(?:when|if).*(?:laundry|wash|dryer|dry|dishwasher|dishes)"
        r".*(?:done|finish|complete|ready)",
        normalized,
    )
    if appliance_match:
        try:
            from . import appliance_monitor
            if appliance_monitor.is_running():
                st = appliance_monitor.status()
                tracking = [
                    f"{s['friendly_name']} ({s['phase']})"
                    for s in st.get("sensors", {}).values()
                ]
                if tracking:
                    return LocalResult(
                        text=(
                            f"Already on it{addr}. I'm monitoring "
                            f"{', '.join(tracking)}. I'll announce when "
                            f"the cycle completes."
                        ),
                        success=True,
                    )
                return LocalResult(
                    text=(
                        f"I'm monitoring for appliance cycles{addr}, "
                        f"but I haven't found any power sensors matching "
                        f"your appliances yet. Make sure the power monitoring "
                        f"sensor has 'washer', 'dryer', or 'dishwasher' "
                        f"in its name."
                    ),
                    success=True,
                )
            else:
                return LocalResult(
                    text=(
                        f"The appliance monitor isn't running at the moment"
                        f"{addr}. It starts automatically with the "
                        f"observer. Check if the observer is enabled."
                    ),
                    success=True,
                )
        except Exception:
            return LocalResult(
                text=(
                    f"I'll keep an eye on it{addr}. If I have a "
                    f"power sensor for that appliance, I'll announce when "
                    f"the cycle finishes."
                ),
                success=True,
            )

    # Follow-up ("also turn off the kitchen")
    if _ctx_fresh() and re.search(r"^(?:also|and|now)\s+", normalized):
        stripped = re.sub(r"^(?:also|and|now)\s+", "", normalized)
        result = await try_local(hass, stripped, honorific, force=force,
                                 device_id=device_id)
        if result and result.handled:
            return result

    # Low complexity bare entity name → state query.
    # Guard: only attempt this for SHORT, non-interrogative phrases. A bare
    # entity name is something like "kitchen lights" or "front door" — not a
    # question or a sentence about Nova itself. Without this guard, phrases
    # like "what are your capabilities" get fed wholesale to the resolver,
    # which wastes a full registry scan and (previously) logged noise.
    if complexity < 40 and _looks_like_entity_name(normalized):
        resolved = _find_entity(hass, normalized, None)
        if isinstance(resolved, _AmbiguousEntity):
            return LocalResult(
                text=_build_local_clarification(resolved.candidates, addr),
                success=False,
            )
        if resolved:
            entity_id, fname = resolved
            state = hass.states.get(entity_id)
            if state:
                return LocalResult(text=f"{fname} is currently {state.state}{addr}.", success=True)

    _LOGGER.debug("Local: no match for '%s' (complexity=%d) — LLM", text[:60], complexity)
    return None


def _looks_like_entity_name(text: str) -> bool:
    """
    Heuristic: is this short phrase plausibly a bare device/entity name
    (vs. a question or a sentence)? Used to gate the speculative state-query
    lookup so conversational input never hits the entity resolver.
    """
    t = text.strip().lower()
    if not t:
        return False
    # Questions are never bare entity names.
    if t.endswith("?"):
        return False
    words = t.split()
    # Too long to be a device name.
    if len(words) > 4:
        return False
    # Interrogatives / conversational openers.
    _QUESTION_STARTS = (
        "what", "who", "when", "where", "why", "how", "is", "are", "can",
        "could", "would", "should", "do", "does", "did", "will", "tell",
        "explain", "describe", "give", "show me how", "help",
    )
    if words[0] in _QUESTION_STARTS:
        return False
    # Phrases about Nova itself ("your X", "you Y") aren't device names.
    if "your" in words or "you" in words:
        return False
    return True
