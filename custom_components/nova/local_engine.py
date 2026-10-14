"""Nova's local answers: requests handled with no model at all.

try_local is asked first for every typed or spoken request. It answers what
it can from Home Assistant directly and returns None for anything that needs
the agent:

* fixed replies (time, date, greetings, thanks, house status);
* questions about the house (who is home, what is open, which lights are
  on, energy, temperature, weather, what is in a room, Home Assistant's own
  status) and "repeat that";
* commands for one device, every device of a kind, or every device of a
  kind in a room;
* scenes and scripts by name, and "goodnight";
* the appliance monitor, "and ..." follow ups, and a bare device name.

Every command first asks policy whether it may run without a confirmation.
Anything protected (an unlock, opening a garage, a scene whose contents are
unknown) is left to the agent, which can ask. The request's device is passed
along, so a spoken request from a voice satellite gets the satellite rules.

complexity scoring decides when a request is too open ended to try here.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Optional


_LOGGER = logging.getLogger(__name__)


# ── varied confirmations ────────────────────────────────────────────────────

# The last few openers used, so the same one is not said twice running.
_recent_acks: list[str] = []
_ACK_MEMORY = 4


def _pick(options: list[str]) -> str:
    """One of options, avoiding the last few picked when possible."""
    if not options:
        return ""
    unused = [o for o in options if o not in _recent_acks]
    choice = random.choice(unused or options)
    _recent_acks.append(choice)
    del _recent_acks[:-_ACK_MEMORY]
    return choice


def _addr(honorific: str) -> str:
    """", sir" style address, or nothing when there is nobody to address
    (honorific.py can return "")."""
    return f", {honorific}" if honorific else ""


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _name_of(state, fallback: str = "") -> str:
    return state.attributes.get("friendly_name", fallback or state.entity_id)


@dataclass
class LocalResult:
    """What try_local did. handled False means the agent should take it.

    repeat_of_id is set only by "repeat that". conversation.py uses it to
    record the reply as a repeat in Spoken History, and only when the reply
    goes out through a path that confirms it was spoken."""
    text: str
    success: bool
    handled: bool = True
    repeat_of_id: Optional[int] = None


# ── follow up context ───────────────────────────────────────────────────────

_FOLLOW_UP_SECONDS = 120


class _ConvCtx:
    """What the last local command touched, for "and ..." follow ups."""
    last_ts: float = 0.0
    last_entity = last_area = last_domain = last_action = ""


_CTX = _ConvCtx()


def _update_ctx(**fields) -> None:
    """Remember the non empty fields given (entity, area, domain, action)
    and stamp the time."""
    for key, value in fields.items():
        if value:
            setattr(_CTX, "last_" + key, value)
    _CTX.last_ts = time.time()


def _ctx_fresh() -> bool:
    return time.time() - _CTX.last_ts < _FOLLOW_UP_SECONDS


def _normalize(text: str) -> str:
    """Lower case, trailing punctuation dropped, spaces collapsed."""
    text = re.sub(r"[.,!?;:]+$", "", text.lower().strip())
    return re.sub(r"\s+", " ", text)


# ── patterns ────────────────────────────────────────────────────────────────

_THE = r"(?:the\s+)?"
_IN_ROOM = r"(?:\s+(?:in|on)\s+(?:the\s+)?(.+))?$"

# (pattern, action, domain hint), tried in order with re.search. Locks: the
# unlock pattern comes first, and (?<!un) keeps "lock" from matching inside
# "unlock".
_INTENT_PATTERNS = [
    (r"turn\s+on\s+" + _THE + r"(.+?)(?:\s+light(?:s)?)?$", "turn_on", "light"),
    (r"turn\s+off\s+" + _THE + r"(.+?)(?:\s+light(?:s)?)?$", "turn_off", "light"),
    (r"switch\s+on\s+" + _THE + r"(.+?)(?:\s+light(?:s)?)?$", "turn_on", "light"),
    (r"switch\s+off\s+" + _THE + r"(.+?)(?:\s+light(?:s)?)?$", "turn_off", "light"),
    (r"lights?\s+on\s+(?:in\s+)?" + _THE + r"(.+)$", "turn_on", "light"),
    (r"lights?\s+off\s+(?:in\s+)?" + _THE + r"(.+)$", "turn_off", "light"),
    (r"dim\s+" + _THE + r"(.+?)(?:\s+light(?:s)?)?\s+to\s+(\d+)", "dim", "light"),
    (r"set\s+" + _THE + r"(.+?)(?:\s+light(?:s)?)?\s+(?:brightness\s+)?to\s+(\d+)",
     "dim", "light"),
    (r"brighten\s+" + _THE + r"(.+)", "brighten", "light"),
    (r"turn\s+on\s+" + _THE + r"(.+?)(?:\s+switch)?$", "turn_on", "switch"),
    (r"turn\s+off\s+" + _THE + r"(.+?)(?:\s+switch)?$", "turn_off", "switch"),
    (r"unlock\s+" + _THE + r"(.+?)(?:\s+door)?$", "unlock", "lock"),
    (r"(?<!un)lock\s+" + _THE + r"(.+?)(?:\s+door)?$", "lock", "lock"),
    (r"open\s+" + _THE + r"(.+?)(?:\s+(?:door|gate|cover|garage))?$", "open", "cover"),
    (r"close\s+" + _THE + r"(.+?)(?:\s+(?:door|gate|cover|garage))?$", "close", "cover"),
    (r"set\s+" + _THE + r"(?:temperature|thermostat|temp)\s+(?:to|at)\s+(\d+)",
     "set_temp", "climate"),
    (r"set\s+" + _THE + r"(.+?)\s+(?:temperature|thermostat|temp)\s+(?:to|at)\s+(\d+)",
     "set_temp_named", "climate"),
    (r"(?:make\s+it|set\s+it)\s+(?:to\s+)?(\d+)\s*(?:degrees|°)?", "set_temp", "climate"),
    (r"turn\s+on\s+" + _THE + r"(.+?)(?:\s+fan)?$", "turn_on", "fan"),
    (r"turn\s+off\s+" + _THE + r"(.+?)(?:\s+fan)?$", "turn_off", "fan"),
    (r"(?:pause|stop)\s+" + _THE + r"(?:music|media|tv|playback)" + _IN_ROOM,
     "media_pause", "media_player"),
    (r"(?:resume|play|unpause)\s+" + _THE + r"(?:music|media|tv|playback)" + _IN_ROOM,
     "media_play", "media_player"),
    (r"(?:volume\s+up|turn\s+(?:it|the\s+volume)\s+up)" + _IN_ROOM,
     "volume_up", "media_player"),
    (r"(?:volume\s+down|turn\s+(?:it|the\s+volume)\s+down)" + _IN_ROOM,
     "volume_down", "media_player"),
    (r"(?:set\s+)?volume\s+(?:to\s+)?(\d+)(?:\s+(?:percent|%?))?" + _IN_ROOM,
     "volume_set", "media_player"),
    (r"mute(?:\s+" + _THE + r"(?:tv|speaker|media))?" + _IN_ROOM, "mute", "media_player"),
    (r"unmute(?:\s+" + _THE + r"(?:tv|speaker|media))?" + _IN_ROOM,
     "unmute", "media_player"),
    (r"(?:skip|next)\s+(?:track|song)" + _IN_ROOM, "media_next", "media_player"),
    (r"(?:activate|run|trigger|start|execute)\s+" + _THE + r"(.+?)(?:\s+scene)?$",
     "scene", None),
    (r"(?:what(?:'s| is)\s+the\s+)?temperature\s+(?:in\s+)?" + _THE + r"(.+)",
     "query_temp", None),
    (r"(?:what(?:'s| is)\s+the\s+)?(.+?)(?:\s+temperature)", "query_temp", None),
    (r"(?:is|are)\s+" + _THE + r"(.+?)\s+(?:on|off|open|closed|locked|unlocked)",
     "query_state", None),
    (r"(?:what(?:'s| is)\s+the\s+)?status\s+of\s+" + _THE + r"(.+)", "query_state", None),
    (r"what\s+time\s+is\s+it", "query_time", None),
    (r"what(?:'s| is)\s+the\s+time", "query_time", None),
    (r"what(?:'s| is)\s+today(?:'s)?\s+date", "query_date", None),
    (r"^(?:hey|hi|hello|good\s+(?:morning|afternoon|evening))(?:\s+nova)?[.!]?$",
     "greeting", None),
    (r"^(?:thanks|thank\s+you|cheers)(?:\s+.*)?$", "thanks", None),
    (r"^(?:you'?re\s+welcome|no\s+problem|no\s+worries)", "thanks", None),
    (r"(?:home|house)\s+status", "status", None), (r"how(?:'s| is)\s+the\s+house", "status", None),
    (r"system\s+status", "status", None), (r"run\s+(?:a\s+)?diagnostic", "status", None),
]

# The same table without the scene pattern, for single device commands.
_DEVICE_PATTERNS = [entry for entry in _INTENT_PATTERNS if entry[1] != "scene"]

# Replies that need no device.
_FIXED_REPLIES = ("query_time", "query_date", "greeting", "thanks", "status")

_ALL = r"(?:all\s+)?" + _THE
_TAIL_AREA = r"\s+(?:in|on)\s+" + _THE + r"(.+)$"

# (pattern, action, domain, scope): every device of a kind, or every device
# of a kind in a named room ("area"). "all" means every light, switch and fan
# that is on.
_BULK_PATTERNS = [
    (r"turn\s+off\s+" + _ALL + r"lights?$", "turn_off", "light", None),
    (r"turn\s+on\s+" + _ALL + r"lights?$", "turn_on", "light", None),
    (r"turn\s+off\s+" + _ALL + r"lights?" + _TAIL_AREA, "turn_off", "light", "area"),
    (r"turn\s+on\s+" + _ALL + r"lights?" + _TAIL_AREA, "turn_on", "light", "area"),
    (r"unlock\s+" + _ALL + r"doors?$", "unlock", "lock", None),
    (r"(?<!un)lock\s+" + _ALL + r"doors?$", "lock", "lock", None),
    (r"close\s+" + _ALL + r"(?:covers?|blinds?|shades?)$", "close", "cover", None),
    (r"open\s+" + _ALL + r"(?:covers?|blinds?|shades?)$", "open", "cover", None),
    (r"turn\s+off\s+" + _ALL + r"fans?$", "turn_off", "fan", None),
    (r"turn\s+on\s+" + _ALL + r"fans?$", "turn_on", "fan", None),
    (r"turn\s+(?:off|out)\s+everything$", "turn_off", "all", None),
]

# "Home" as presence, never the start of "Home Assistant".
_HOME = r"(?:at\s+)?home\b(?!\s*assistant)"
_NOW = r"(?:currently\s+|still\s+|right\s+now\s+)?"
_HEALTH_WORDS = ("reach|reachable|connect|connected|connection|access|available|online|"
                 "offline|up(?!\\s+to\\s+date)|down|running|responding|respond|working|"
                 "alive|status|health|healthy|talk\\s+to")
_PLATFORM_HEALTH = (r"^(?=.*\bhome\s?assistant\b)(?=.*\b(?:" + _HEALTH_WORDS + r")\b)")

# (pattern, question kind), tried in order with re.search.
#
# * A question about reaching Home Assistant gets a status answer, and no
#   presence pattern accepts "home" followed by "assistant".
# * Presence needs presence grammar; "welcome home" is not a question.
# * "what's open" needs its lead in (once a bare "unlocked" anywhere, even
#   inside a complaint quoting an alert, took this shortcut).
# * The temperature with no room named means indoors (8.49.0); it once read
#   "what's the" as a device name.
_QUERY_PATTERNS = [
    (r"(?:repeat\s+that|say\s+that\s+again|what\s+did\s+you\s+just\s+say|"
     r"repeat\s+(?:your\s+)?last\s+announcement)\b", "repeat_last"),
    (_PLATFORM_HEALTH, "platform_status"),
    (r"^(?:so\s+|and\s+)?who(?:'s|s|\s+is|\s+are)\s+" + _NOW + _HOME, "who_home"),
    (r"^(?:is\s+|are\s+)?(?:there\s+)?(?:anyone|anybody|someone|somebody|everyone|everybody)"
     r"\s+" + _NOW + _HOME, "who_home"),
    (r"^how\s+many\s+(?:people|persons|of\s+us)\s+(?:are\s+)?" + _NOW + _HOME,
     "count_home"),
    (r"^is\s+([a-z][a-z'\- ]{0,40}?)\s+" + _NOW + _HOME + "$", "person_home"),
    (r"(?:what(?:'s| is)\s+)(?:open|unlocked)\b", "what_open"),
    (r"(?:are\s+)?(?:any|which)\s+(?:doors?|windows?)\s+open", "what_open"),
    (r"(?:list|show(?:\s+me)?|tell\s+me(?:\s+which)?|name|which|what)\s+(?:all\s+)?"
     r"(?:of\s+)?(?:the\s+)?lights?\s+(?:that\s+|which\s+)?(?:are\s+)?"
     r"(?:currently\s+|still\s+|now\s+)?(?:turned\s+)?on\b", "lights_on"),
    (r"(?:are\s+)?(?:any|which)\s+(?:lights?)\s+on", "lights_on"),
    (r"(?:how\s+many)\s+lights?\s+(?:are\s+)?on", "lights_on"),
    (r"(?:what(?:'s| is)\s+(?:the\s+)?)?(?:energy|power)\s+(?:usage|consumption)", "energy"),
    (r"^(?:(?:hey\s+)?nova,?\s+)?(?:what(?:'s| is)\s+)?(?:the\s+)?(?:current\s+)?"
     r"(?:indoor\s+)?temperature(?:\s+(?:inside|indoors|at\s+home|in\s+the\s+house))?"
     r"(?:\s+now)?$", "indoor_temp"),
    (r"(?:what(?:'s| is)\s+(?:the\s+)?)?weather", "weather"),
    (r"(?:what(?:'s| is)\s+(?:it\s+)?)?(?:like\s+)?outside", "weather"),
    (r"(?:how\s+(?:warm|cold|hot))\s+is\s+it", "weather"),
    (r"(?:what\s+)?(?:devices?|entities?)\s+(?:are\s+)?(?:in|at)\s+(?:the\s+)?(.+)",
     "area_devices"),
]

# "Don't change anything", "without touching it", "just list ...": the
# person wants information only, so nothing is switched here.
_CHANGE_VERBS = ("change|changing|touch|touching|turn|turning|switch|switching|"
                 "adjust|adjusting|alter|altering")
_READ_ONLY_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|without)\s+(?:" + _CHANGE_VERBS + r")\b"
    r"|\b(?:read[\s-]?only|just\s+(?:list|tell|show))\b")

_GOODNIGHT_RE = r"^good\s*night(?:\s+nova)?$"
_FOLLOW_UP_RE = r"^(?:also|and|now)\s+"
_APPLIANCE_RE = (r"(?:let\s+me\s+know|tell\s+me|notify\s+me|alert\s+me)"
                 r".*(?:when|if).*(?:laundry|wash|dryer|dry|dishwasher|dishes)"
                 r".*(?:done|finish|complete|ready)")


# ── complexity ──────────────────────────────────────────────────────────────

# A request scoring at or above this goes to the agent.
_AGENT_SCORE = 70
_HARD_WORDS = ("explain", "why", "how does", "what do you think", "write", "compose",
               "draft", "create a", "help me", "analyze", "compare", "recommend",
               "suggest", "tell me about", "what happened", "story", "code", "script",
               "program", "debug", "fix this", "plan", "schedule", "strategy",
               "brainstorm", "summarize", "translate", "calculate")
_LINK_WORDS = ("because", "however", "although")
_COMMAND_WORDS = ("turn on", "turn off", "lock", "unlock", "open", "close", "dim",
                  "brighten", "set temp", "volume", "pause", "play")
_SMALL_TALK_WORDS = ("what time", "who's home", "how many lights", "what's open",
                     "temperature", "status", "good morning", "good night", "hello",
                     "thanks")


def _contains_word(text: str, phrase: str) -> bool:
    """phrase in text on word boundaries, so "lock" is not found inside
    "unlocked". A substring test once let a complaint quoting an "unlocked"
    alert score as a simple lock command."""
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def _any_word(text: str, phrases) -> bool:
    return any(_contains_word(text, p) for p in phrases)


def score_complexity(text: str) -> int:
    """0 to 100; higher needs the agent. Starts at 20, each signal applies
    at most once."""
    lowered = text.lower()
    words = len(text.split())
    score = 20
    score += 40 if _any_word(lowered, _HARD_WORDS) else 0
    score += 15 if words > 20 else 0
    score += 10 if "?" in text and words > 10 else 0
    score += 10 if _any_word(lowered, _LINK_WORDS) else 0
    score -= 20 if _any_word(lowered, _COMMAND_WORDS) else 0
    score -= 15 if _any_word(lowered, _SMALL_TALK_WORDS) else 0
    return min(100, max(0, score))


# ── finding devices ─────────────────────────────────────────────────────────

def _bigrams(text: str) -> set:
    if len(text) < 2:
        return {text}
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _fuzzy_score(a: str, b: str) -> float:
    """0 to 100 likeness of two names: the best of character pair overlap
    (Dice), shared words, and one name containing the other."""
    if a == b:
        return 100.0
    if not (a and b):
        return 0.0
    pairs_a, pairs_b = _bigrams(a), _bigrams(b)
    dice = (2.0 * len(pairs_a & pairs_b)) / (len(pairs_a) + len(pairs_b)) * 100
    words_a, words_b = set(a.split()), set(b.split())
    shared = 0.0
    if words_a and words_b:
        shared = len(words_a & words_b) / max(len(words_a), len(words_b)) * 100
    inside = 0.0
    if a in b:
        inside = len(a) / len(b) * 80
    elif b in a:
        inside = len(b) / len(a) * 80
    return max(dice, shared, inside)


# What speech to text often writes, and what was meant.
_STT_CORRECTIONS = {
    "lamp": "light", "lamps": "lights", "lite": "light", "lites": "lights",
    "nite": "night", "nitestand": "nightstand", "night stand": "nightstand",
    "bed side": "bedside", "bed lamp": "bedside light",
    "tv": "television", "teevee": "tv", "a.c.": "ac", "air con": "air conditioner",
    "thermos": "thermostat", "thermo": "thermostat",
    "bed room": "bedroom", "living room": "living_room", "bath room": "bathroom",
    "dining room": "dining_room", "down stairs": "downstairs", "up stairs": "upstairs",
    "front porch": "front_porch", "back yard": "backyard",
}

# Words dropped from the end of a spoken device name, in this order.
_NAME_SUFFIXES = ("light", "lights", "lamp", "lamps", "switch", "switches", "fan",
                  "fans", "lock", "locks", "door", "doors", "cover", "covers",
                  "thermostat", "sensor", "please", "now", "for me", "nova")
_SEARCH_DOMAINS = ["light", "switch", "lock", "cover", "climate", "fan", "media_player",
                   "sensor", "binary_sensor"]
_MIN_SCORE = 25
_NEAR_TIE = 0.75


class _AmbiguousEntity:
    """Returned by _find_entity instead of (entity_id, name) when two or more
    devices fit. It is truthy and cannot be unpacked, so a caller has to
    check for it before using the result. candidates holds at least two
    entries, one per entity."""

    def __init__(self, candidates: list[dict]):
        by_id: dict = {}
        for cand in candidates:
            by_id.setdefault(cand["entity_id"], cand)
        self.candidates = list(by_id.values())


def _build_local_clarification(candidates: list[dict], addr: str) -> str:
    """A fixed question naming the first two candidates. Names that repeat
    get their entity id, so it never asks "Lamp or Lamp?"."""
    names = [c.get("friendly_name") or c["entity_id"] for c in candidates]
    labels = [f"{name} ({c['entity_id']})" if names.count(name) > 1 else name
              for c, name in zip(candidates, names)]
    if len(labels) < 2:
        return f"I found more than one possible match{addr} — could you be more specific?"
    return f"I found more than one match{addr} — did you mean {labels[0]} or {labels[1]}?"


def _read_aliases() -> dict:
    """Names the person taught through the agent's remember tool. Reads a
    file, so call it in the executor. {} on any problem."""
    try:
        from . import paths
        from .persistence.files import OK, read_json
        read = read_json(paths.learned_file())
        if read.status != OK or not isinstance(read.value, dict):
            return {}
        aliases = read.value.get("alias", {})
        return aliases if isinstance(aliases, dict) else {}
    except Exception:
        return {}


def _strip_name(fragment: str) -> str:
    name = fragment.lower().strip()
    for suffix in _NAME_SUFFIXES:
        name = re.sub(rf"\s+{suffix}$", "", name)
    return name.strip()


def _stt_corrected(name: str) -> str:
    for heard, meant in _STT_CORRECTIONS.items():
        if heard in name:
            name = name.replace(heard, meant)
    return name


def _area_members(hass, area_id: str, domain: Optional[str], ent_reg, dev_reg):
    """Registry entries in the area, directly or through their device,
    optionally of one domain."""
    for entry in ent_reg.entities.values():
        if domain is not None and entry.domain != domain:
            continue
        inside = entry.area_id == area_id
        if not inside and entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            inside = bool(device and device.area_id == area_id)
        if inside:
            yield entry


def _registries(hass):
    from homeassistant.helpers import (
        area_registry as areg, device_registry as dr, entity_registry as er)
    return areg.async_get(hass), er.async_get(hass), dr.async_get(hass)


def _first_area(area_reg, name: str):
    wanted = name.lower()
    return next((a for a in area_reg.async_list_areas() if wanted in a.name.lower()), None)


class _Scores:
    """Each entity's best score across every matching pass, so one entity
    can never be both the winner and its own runner up."""

    def __init__(self) -> None:
        self.best: dict = {}

    def offer(self, entity_id: str, name: str, score: float) -> None:
        current = self.best.get(entity_id)
        if current is None or score > current["score"]:
            self.best[entity_id] = {"entity_id": entity_id, "friendly_name": name,
                                    "score": score}

    def top(self) -> float:
        return max((c["score"] for c in self.best.values()), default=0)

    def ranked(self) -> list:
        return sorted(self.best.values(), key=lambda c: (-c["score"], c["entity_id"]))


def _score_by_name(hass, fragment: str, domains, scores: _Scores) -> dict:
    """Score every entity against fragment. Exact name matches are returned
    (by entity id); the rest go into scores, by the first rule that fits:
    contained in the name, all its words in the name, fuzzy likeness over 55,
    or contained in the entity id."""
    exact: dict = {}
    words = set(fragment.split())
    as_id = fragment.replace(" ", "_")
    for domain in domains:
        for state in hass.states.async_all(domain):
            eid = state.entity_id
            shown = _name_of(state, eid)
            name = (state.attributes.get("friendly_name") or "").lower()
            name_words = set(name.split())
            if name == fragment:
                exact.setdefault(eid, {"entity_id": eid, "friendly_name": shown})
            elif fragment in name:
                scores.offer(eid, shown, len(fragment) / max(len(name), 1) * 100)
            elif words and words <= name_words:
                scores.offer(eid, shown, len(words) / max(len(name_words), 1) * 95)
            elif (likeness := _fuzzy_score(fragment, name)) > 55:
                scores.offer(eid, shown, likeness)
            elif as_id in eid:
                scores.offer(eid, shown, len(as_id) / max(len(eid), 1) * 80)
    return exact


def _score_corrected(hass, corrected: str, domains, scores: _Scores) -> None:
    for domain in domains:
        for state in hass.states.async_all(domain):
            name = (state.attributes.get("friendly_name") or "").lower()
            shown = _name_of(state)
            if corrected in name:
                scores.offer(state.entity_id, shown,
                             len(corrected) / max(len(name), 1) * 90)
            likeness = _fuzzy_score(corrected, name)
            if likeness > 55:
                scores.offer(state.entity_id, shown, likeness)


def _by_area(hass, fragment: str, domain: str):
    """The one device of the domain in the first area whose name contains
    fragment, an _AmbiguousEntity for several, or None. An area with none
    moves on to the next matching area."""
    try:
        area_reg, ent_reg, dev_reg = _registries(hass)
        for area in area_reg.async_list_areas():
            if fragment not in area.name.lower():
                continue
            found: dict = {}
            for entry in _area_members(hass, area.id, domain, ent_reg, dev_reg):
                state = hass.states.get(entry.entity_id)
                if state:
                    found.setdefault(entry.entity_id, {
                        "entity_id": entry.entity_id,
                        "friendly_name": _name_of(state, entry.entity_id)})
            if len(found) > 1:
                _LOGGER.info("Entity resolve: '%s' ambiguous — %d entities in area",
                             fragment, len(found))
                return _AmbiguousEntity(list(found.values()))
            if found:
                only = next(iter(found.values()))
                return only["entity_id"], only["friendly_name"]
    except Exception:
        pass
    return None


def _find_entity(hass, name_fragment, domain_hint=None, aliases=None):
    """The device a spoken name means: (entity_id, friendly_name), an
    _AmbiguousEntity when more than one fits, or None.

    In order: a taught alias (always unique); exact friendly names (several
    is ambiguous); then the best score by name (see _score_by_name), with a
    second pass on the speech to text correction when nothing scored 50; then
    with a domain hint and still nothing, the devices in a matching area. A
    winner needs over 25, and a runner up within three quarters of it makes
    the answer ambiguous."""
    fragment = _strip_name(name_fragment)
    if not fragment:
        return None

    if aliases and fragment in aliases:
        target = aliases[fragment]
        state = hass.states.get(target)
        if state:
            _LOGGER.info("Entity resolve: alias '%s' → %s", fragment, target)
            return target, _name_of(state, target)

    domains = [domain_hint] if domain_hint else _SEARCH_DOMAINS
    scores = _Scores()
    exact = _score_by_name(hass, fragment, domains, scores)
    if len(exact) > 1:
        _LOGGER.info("Entity resolve: '%s' ambiguous — %d exact matches",
                     fragment, len(exact))
        return _AmbiguousEntity(list(exact.values()))
    if exact:
        only = next(iter(exact.values()))
        _LOGGER.info("Entity resolve: exact '%s' → %s", fragment, only["entity_id"])
        return only["entity_id"], only["friendly_name"]

    if scores.top() < 50:
        corrected = _stt_corrected(fragment)
        if corrected != fragment:
            _LOGGER.info("Entity resolve: STT correction '%s' → '%s'", fragment, corrected)
            _score_corrected(hass, corrected, domains, scores)

    if not scores.best and domain_hint:
        in_area = _by_area(hass, fragment, domain_hint)
        if in_area is not None:
            return in_area

    ranked = scores.ranked()
    if ranked and ranked[0]["score"] > _MIN_SCORE:
        top = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None
        if (runner and runner["score"] > _MIN_SCORE
                and runner["score"] / top["score"] >= _NEAR_TIE):
            _LOGGER.info("Entity resolve: '%s' ambiguous — top=%.0f second=%.0f",
                         fragment, top["score"], runner["score"])
            return _AmbiguousEntity([top, runner])
        _LOGGER.info("Entity resolve: '%s' → %s (score=%.0f)",
                     fragment, top["entity_id"], top["score"])
        return top["entity_id"], top["friendly_name"]

    # Not finding a device is normal: the agent searches more widely.
    _LOGGER.debug("Entity resolve unmatched: '%s' (domain=%s)", fragment, domain_hint)
    return None


def _find_entities_in_area(hass, area_name, domain):
    """(entity_id, name) of every device of the domain in the first area
    whose name contains area_name. Excluded devices take no part in room
    commands, though they can still be named one by one."""
    found = []
    try:
        try:
            from .entity_filter import is_excluded
        except Exception:
            def is_excluded(_hass, _eid):
                return False
        area_reg, ent_reg, dev_reg = _registries(hass)
        area = _first_area(area_reg, area_name)
        if area is None:
            return []
        for entry in _area_members(hass, area.id, domain, ent_reg, dev_reg):
            if is_excluded(hass, entry.entity_id):
                continue
            state = hass.states.get(entry.entity_id)
            if state:
                found.append((entry.entity_id, _name_of(state, entry.entity_id)))
    except Exception:
        pass
    return found


def _find_scene_or_script(hass, name_fragment):
    """(entity_id, name, "scene" or "script") for the first scene, then
    script, whose name equals or contains the fragment or whose entity id
    contains it. None when nothing fits."""
    wanted = name_fragment.lower().strip()
    for suffix in ("scene", "script", "routine", "mode"):
        wanted = re.sub(rf"\s+{suffix}$", "", wanted)
    wanted = wanted.strip()
    as_id = wanted.replace(" ", "_")
    for kind in ("scene", "script"):
        for state in hass.states.async_all(kind):
            name = (state.attributes.get("friendly_name") or "").lower()
            if wanted in name or as_id in state.entity_id:
                return state.entity_id, _name_of(state), kind
    return None


# ── policy ──────────────────────────────────────────────────────────────────

# The service each simple action calls; turn_on, turn_off and toggle use the
# device's own domain.
_ACTION_SERVICES = {
    "lock": ("lock", "lock"), "unlock": ("lock", "unlock"),
    "open": ("cover", "open_cover"), "close": ("cover", "close_cover"),
    "dim": ("light", "turn_on"), "brighten": ("light", "turn_on"),
    **{action: ("media_player", service) for action, service in (
        ("media_pause", "media_pause"), ("media_play", "media_play"),
        ("media_next", "media_next_track"), ("volume_up", "volume_up"),
        ("volume_down", "volume_down"), ("mute", "volume_mute"),
        ("unmute", "volume_mute"))},
}
_ACTION_EXTRA = {"brighten": {"brightness_pct": 100}, "mute": {"is_volume_muted": True},
                 "unmute": {"is_volume_muted": False}}
_OWN_DOMAIN = ("turn_on", "turn_off", "toggle")
_CLIMATE = ("set_temp", "set_temp_named")


def _service_for(action, entity_id):
    """(domain, service) an action would call, or None when it does not
    actuate anything. Matches _execute_action, so the policy check asks
    about exactly what would run."""
    if action in _OWN_DOMAIN:
        return (entity_id.split(".")[0] if entity_id else ""), action
    if action in _CLIMATE:
        return "climate", "set_temperature"
    if action == "volume_set":
        return "media_player", "volume_set"
    return _ACTION_SERVICES.get(action)


def _policy_requires_confirmation(hass, domain, service, entity_id="",
                                  device_id=None) -> bool:
    """Whether policy would stop this without a confirmation. The request's
    device goes with it, so a voice satellite gets its own rules (an unlock
    or open needs a phone tap); with no device nothing changes.

    If policy cannot answer, anything above low risk is left to the agent,
    which runs the real confirmation, so a failure here can never let a
    protected action through. Low risk conveniences still run."""
    try:
        from . import policy
    except Exception:
        return True
    try:
        verdict = policy.authorize_now(hass, policy.AuthorityRequest(
            domain, service, entity_id, device_id=device_id or ""))
        return not verdict.allowed
    except Exception as exc:
        _LOGGER.warning("Local: confirmation check failed for %s.%s (%s)",
                        domain, service, exc)
    try:
        risk, _ = policy.classify(domain, service, entity_id)
    except Exception:
        return True
    return risk != "low"


def _needs_confirmation(hass, action, entity_id="", device_id=None) -> bool:
    """Whether a local action must go to the agent for a confirmation, so the
    local path is never a second, unguarded way to unlock a door."""
    service = _service_for(action, entity_id)
    if service is None:
        return False
    return _policy_requires_confirmation(hass, *service, entity_id, device_id)


def _needs_confirmation_domain(hass, domain, service, entity_id="", device_id=None) -> bool:
    """The same check for a known (domain, service), such as a scene."""
    return _policy_requires_confirmation(hass, domain, service, entity_id, device_id)


# ── acting ──────────────────────────────────────────────────────────────────

async def _execute_action(hass, action, entity_id, args, device_id=None):
    """Send one action. True when the call went out. Simple actions ask
    policy once more just before the call (8.24.0); callers have already
    sent protected ones to the agent, so this only stops what slipped
    through. Never raises."""
    try:
        if action in _CLIMATE:
            temperature = args.get("temperature")
            if not temperature:
                return False
            await hass.services.async_call(
                "climate", "set_temperature",
                {"entity_id": entity_id, "temperature": float(temperature)}, blocking=True)
            return True
        if action == "volume_set":
            level = args.get("volume_level", 50)
            await hass.services.async_call(
                "media_player", "volume_set",
                {"entity_id": entity_id, "volume_level": level / 100.0}, blocking=True)
            return True
        if action in _OWN_DOMAIN:
            domain, service = entity_id.split(".")[0], action
        elif action in _ACTION_SERVICES:
            domain, service = _ACTION_SERVICES[action]
        else:
            return False
        from . import policy
        verdict = policy.authorize_now(hass, policy.AuthorityRequest(
            domain, service, entity_id, device_id=device_id or ""))
        if not verdict.allowed:
            return False
        data = {"entity_id": entity_id, **_ACTION_EXTRA.get(action, {})}
        if action == "dim" and "brightness_pct" in args:
            data["brightness_pct"] = args["brightness_pct"]
        await hass.services.async_call(domain, service, data, blocking=True)
        return True
    except Exception as exc:
        _LOGGER.warning("Local action failed for %s: %s", entity_id, exc)
        return False


async def _logged(hass, row_id, result, reason_code=None) -> None:
    from . import action_log
    if reason_code is None:
        await hass.async_add_executor_job(lambda: action_log.set_execution(row_id, result))
    else:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(row_id, result, reason_code=reason_code))


def _background_check(hass, entity_id, action, domain, service, row_id) -> None:
    """Hand the action to agent._verify_control, the same retry and report
    the agent's own tools use."""
    from .agent import _verify_control
    hass.async_create_task(_verify_control(
        hass, entity_id, action, domain, service, {"entity_id": entity_id},
        source="local_engine", action_id=row_id))


async def _execute_action_verified(hass, action, entity_id, args, fname, device_id=None):
    """Send one action and say how sure Nova is it worked: "verified",
    "accepted", "unverified" or "error".

    Uses the same entity_verify checks and the same background
    _verify_control as the agent, so both paths mean the same by
    "verified". Logged as a voice control_device request. Commands for many
    devices at once do not come through here: checking each in turn would
    multiply the wait.

    * turn_on / turn_off on a light, switch or fan is read back briefly;
      otherwise (or if it did not land yet) the background check takes over.
      A light, switch or fan that did not land is "unverified", anything
      else "accepted".
    * toggle on a light, switch or fan is read back; it is never retried,
      since a second toggle could undo a slow first one.
    * dim and brighten are read back for the level, never retried.
    * lock, unlock, open and close go to the background check.
    """
    from . import action_log
    domain = entity_id.split(".")[0]
    before = hass.states.get(entity_id)
    before_state = before.state if before else None
    requested = (args.get("brightness_pct") or args.get("temperature")
                 or args.get("volume_level") or action)
    request_id = action_log.new_request_id()
    row_id = await hass.async_add_executor_job(lambda: action_log.start(
        request_id, "control_device", "voice", domain=domain, entity_id=entity_id,
        requested_state=str(requested)))

    if not await _execute_action(hass, action, entity_id, args, device_id=device_id):
        await _logged(hass, row_id, "failed", "service_call_failed")
        return "error"
    await _logged(hass, row_id, "accepted")

    from . import entity_verify
    fast = domain in entity_verify.FAST_VERIFY_DOMAINS

    async def read_back(check) -> bool:
        if await entity_verify.wait_until(check):
            await _logged(hass, row_id, "verified")
            return True
        return False

    if action in ("turn_on", "turn_off"):
        wanted = "on" if action == "turn_on" else "off"
        if fast and await read_back(
                lambda: entity_verify.check_state_once(hass, entity_id, wanted)):
            return "verified"
        _background_check(hass, entity_id, action, domain, action, row_id)
        return "unverified" if fast else "accepted"

    if action == "toggle":
        flipped = {"on": "off", "off": "on"}.get(before_state)
        if not fast or flipped is None:
            return "accepted"
        if await read_back(lambda: entity_verify.check_state_once(hass, entity_id, flipped)):
            return "verified"
        await entity_verify.record_unverified(
            hass, entity_id, "toggle", source="local_engine",
            detail=", not retried automatically because repeating toggle "
                   "could reverse a delayed successful action")
        await _logged(hass, row_id, "unverified", "not_retried_toggle_could_reverse")
        return "unverified"

    if action in ("dim", "brighten"):
        level = args.get("brightness_pct", 100 if action == "brighten" else 50)
        if await read_back(
                lambda: entity_verify.check_brightness_once(hass, entity_id, level)):
            return "verified"
        await entity_verify.record_unverified(
            hass, entity_id, action, source="local_engine",
            detail=(f", requested {level}%, not retried automatically because the "
                    "background verifier cannot validate the requested brightness level"))
        await _logged(hass, row_id, "unverified", "brightness_not_confirmed")
        return "unverified"

    if action in ("lock", "unlock", "open", "close"):
        check_domain, check_service = _ACTION_SERVICES[action]
        _background_check(hass, entity_id, action, check_domain, check_service, row_id)
    return "accepted"


# ── replies ─────────────────────────────────────────────────────────────────

# Sent but not confirmed: wording that never claims it is done.
_ACCEPTED_TEXT = {
    "turn_on": "I've sent the command to turn on {name}{addr}.",
    "turn_off": "I've sent the command to turn off {name}{addr}.",
    "toggle": "I've sent the toggle command to {name}{addr}.",
    "lock": "Locking {name} now{addr}.",
    "unlock": "Unlocking {name} now{addr}.",
    "open": "Opening {name} now{addr}.",
    "close": "Closing {name} now{addr}.",
    "set_temp": "I've sent the command to set the temperature to {temp}°{addr}.",
    "set_temp_named": "I've sent the command to set {name} to {temp}°{addr}.",
    "media_pause": "I've sent the pause command{addr}.",
    "media_play": "I've sent the play command{addr}.",
    "media_next": "I've sent the next-track command{addr}.",
    "volume_up": "I've sent the volume-up command{addr}.",
    "volume_down": "I've sent the volume-down command{addr}.",
    "volume_set": "I've sent the command to set the volume to {vol}%{addr}.",
    "mute": "I've sent the mute command{addr}.",
    "unmute": "I've sent the unmute command{addr}.",
}
# Confirmed: only these actions ever reach "verified".
_DONE_TEXT = {
    "turn_on": "{ack}{addr}. {name} is on.",
    "turn_off": "{ack}{addr}. {name} is off.",
    "toggle": "{ack}{addr}. {name} toggled.",
    "dim": "{ack}{addr}. {name} at {pct}%.",
    "brighten": "{ack}{addr}. {name} at full brightness.",
}
_OPENERS = ["Done", "Right away", "As you wish", "Consider it done", "At once"]


def _resp(action, fname, success, args=None, h="sir", status=None):
    """The reply for one device action, worded by how sure Nova is: failed,
    "unverified" (sent, could not confirm), "accepted" (sent, nothing checks
    it here) or confirmed."""
    addr = _addr(h)
    args = args or {}
    words = action.replace("_", " ")
    if not success:
        return (f"I wasn't able to {words} {fname}{addr} — "
                "there may be a connectivity issue.")
    if status == "unverified":
        if action in ("dim", "brighten"):
            pct = args.get("brightness_pct", 100 if action == "brighten" else "?")
            return (f"I sent the command to set {fname} to {pct}%{addr}, but I "
                    "can't confirm the brightness reached that level yet.")
        return f"I sent the command to {words} {fname}{addr}, but I can't confirm it worked yet."
    if status == "accepted":
        template = _ACCEPTED_TEXT.get(action, "I've sent the {words} command to {name}{addr}.")
        return template.format(name=fname, addr=addr, words=words,
                               temp=args.get("temperature", "?"),
                               vol=args.get("volume_level", "?"))
    ack = _pick(_OPENERS)
    template = _DONE_TEXT.get(action, "{ack}{addr}. {action} applied to {name}.")
    return template.format(ack=ack, addr=addr, name=fname, action=action,
                           pct=args.get("brightness_pct", "?"))


def _greeting(hour: int) -> str:
    if hour < 12:
        return "Good morning"
    return "Good afternoon" if hour < 18 else "Good evening"


def _query_resp(hass, action, entity_id, fname, h="sir"):
    """The reply for a fixed answer or a question about one device; "" for
    anything else."""
    addr = _addr(h)
    if action in ("query_time", "query_date", "greeting"):
        from homeassistant.util import dt as dt_util
        now = dt_util.now()
        if action == "query_time":
            return f"It's currently {now.strftime('%I:%M %p')}{addr}."
        if action == "query_date":
            from .locale_format import format_date
            return f"Today is {format_date(now, hass, include_year=True)}{addr}."
        hello = _greeting(now.hour)
        return _pick([f"{hello}{addr}.", f"{hello}{addr}. What can I do for you?",
                      f"{hello}{addr}. At your disposal."])
    if action == "thanks":
        return _pick([f"Of course{addr}.", f"My pleasure{addr}.", f"Anytime{addr}."])
    if action == "status":
        return _home_status(hass, h)
    if action not in ("query_temp", "query_state"):
        return ""
    state = hass.states.get(entity_id)
    if action == "query_temp":
        if state is None:
            return f"I'm unable to read the temperature for {fname} at the moment{addr}."
        unit = state.attributes.get("unit_of_measurement", "°")
        return f"The temperature in {fname} is currently {state.state}{unit}{addr}."
    if state is None:
        return f"I'm unable to determine the status of {fname} at the moment{addr}."
    return f"{fname} is currently {state.state}{addr}."


def _unlocked_locks(hass) -> list:
    """Unlocked locks, leaving out those lockdown never touches."""
    from .cognitive_core import _lockdown_exempt_locks
    exempt = _lockdown_exempt_locks()
    return [s for s in hass.states.async_all("lock")
            if s.state == "unlocked" and s.entity_id not in exempt]


def _home_status(hass, h="sir"):
    lights = sum(s.state == "on" for s in hass.states.async_all("light"))
    unlocked = len(_unlocked_locks(hass))
    doors = sum(s.attributes.get("device_class") == "door" and s.state == "on"
                for s in hass.states.async_all("binary_sensor"))
    people = sum(s.state == "home" for s in hass.states.async_all("person"))
    parts = [f"All systems nominal{_addr(h)}.", f"{_plural(lights, 'light')} on."]
    parts.append(f"{_plural(unlocked, 'lock')} unlocked." if unlocked
                 else "All locks secured.")
    if doors:
        parts.append(f"{_plural(doors, 'door')} open.")
    parts.append(f"{_plural(people, 'person')} home.")
    return " ".join(parts)


# ── Home Assistant's own status ─────────────────────────────────────────────

_CORE_STATE_WORDS = {"not_running": "not running", "final_write": "shutting down",
                     "stopping": "stopping", "stopped": "stopped", "starting": "starting"}


def _core_state_name(state) -> str:
    """Home Assistant's run state as a lower case name. The real CoreState
    has lower case member names and upper case values, so the member name is
    used; a plain string is just lowered. None counts as running, since this
    code is running inside it."""
    if state is None:
        return "running"
    name = getattr(state, "name", None)
    if not isinstance(name, str):
        name = str(getattr(state, "value", state))
    return name.strip().lower()


def _entity_count(hass):
    try:
        return hass.states.async_entity_ids_count()
    except Exception:
        pass
    try:
        return len(hass.states.async_all())
    except Exception:
        return None


def _platform_status(hass, addr: str) -> str:
    """What this running instance can see of Home Assistant itself: its run
    state, version and entity count. Answering at all means it is up; this
    never claims more."""
    try:
        from homeassistant.const import __version__ as ha_version
        version = f" {ha_version}" if re.match(r"^\d", str(ha_version)) else ""
    except Exception:
        version = ""
    count = _entity_count(hass)
    state = _core_state_name(getattr(hass, "state", None))
    if state != "running":
        shown = _CORE_STATE_WORDS.get(state, state.replace("_", " "))
        return (f"Home Assistant{version} is {shown} right now{addr}, so some devices "
                "may not respond until it finishes.")
    if not count:
        return f"Home Assistant{version} is running{addr}, but I can't see any entities right now."
    noun = "entity" if count == 1 else "entities"
    return (f"Home Assistant{version} is running and responding{addr}. I'm connected to "
            f"it and can see {count} {noun}.")


# ── questions about the house ───────────────────────────────────────────────

def _and_join(names: list) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _nobody(addr: str) -> str:
    return f"No one appears to be home at the moment{addr}."


def _who_home(hass, addr: str) -> str:
    home = [_name_of(s) for s in hass.states.async_all("person") if s.state == "home"]
    if not home:
        return _nobody(addr)
    verb = "is" if len(home) == 1 else "are"
    return f"{_and_join(home)} {verb} currently home{addr}."


def _count_home(hass, addr: str) -> str:
    home = sorted(_name_of(s) for s in hass.states.async_all("person") if s.state == "home")
    if not home:
        return _nobody(addr)
    if len(home) == 1:
        return f"One person is home{addr}: {home[0]}."
    return f"{len(home)} people are home{addr}: {_and_join(home)}."


def _person_home(hass, addr: str, asked: str):
    """Where a named person is, or None when no person has that name (the
    agent then takes it)."""
    wanted = " ".join(asked.replace("_", " ").split())
    if not wanted:
        return None
    for state in sorted(hass.states.async_all("person"), key=lambda p: p.entity_id):
        name = str(state.attributes.get("friendly_name") or "")
        slug = state.entity_id.split(".", 1)[1].replace("_", " ")
        if wanted not in (name.lower(), slug):
            continue
        shown = name or state.entity_id
        where = {"home": f"{shown} is home{addr}.",
                 "not_home": f"{shown} is away{addr}.",
                 "unknown": f"I can't tell where {shown} is right now{addr}.",
                 "unavailable": f"I can't tell where {shown} is right now{addr}."}
        return where.get(state.state, f"{shown} is at {state.state}{addr}.")
    return None


def _what_open(hass, addr: str) -> str:
    items = [_name_of(s) for s in hass.states.async_all("binary_sensor")
             if s.state == "on"
             and s.attributes.get("device_class", "") in ("door", "window", "garage_door")]
    items += [_name_of(s) for s in hass.states.async_all("cover") if s.state == "open"]
    items += [_name_of(s) + " (unlocked)" for s in _unlocked_locks(hass)]
    if not items:
        return f"Everything is closed and secured{addr}."
    return f"Currently open or unlocked: {', '.join(items)}."


def _lights_on(hass, addr: str) -> str:
    """Read only. Names come from the presentation helper, which adds the
    area when two lights share a name."""
    from .agent_runtime.presentation import display_names
    on = [s.entity_id for s in hass.states.async_all("light") if s.state == "on"]
    if not on:
        return f"All lights are off{addr}."
    names = display_names(hass, on)
    shown = ", ".join(names.get(eid, eid) for eid in on[:5])
    return f"{_plural(len(on), 'light')} on: {shown}{'...' if len(on) > 5 else ''}."


def _energy(hass, addr: str) -> str:
    for state in hass.states.async_all("sensor"):
        if (state.attributes.get("device_class") == "power"
                and "total" in state.entity_id.lower()):
            unit = state.attributes.get("unit_of_measurement", "W")
            return f"Current power consumption is {state.state} {unit}{addr}."
    return f"I don't have a total power sensor configured{addr}."


def _indoor(hass, addr: str):
    """Up to three thermostats' readings, or None when there are none."""
    rooms = [f"{_name_of(s)} {s.attributes['current_temperature']}°"
             for s in sorted(hass.states.async_all("climate"), key=lambda c: c.entity_id)
             if s.attributes.get("current_temperature") is not None]
    return f"Inside it's {', '.join(rooms[:3])}{addr}." if rooms else None


def _weather(hass, addr: str) -> str:
    for state in hass.states.async_all("weather"):
        temperature = state.attributes.get("temperature", "—")
        humidity = state.attributes.get("humidity", "—")
        return (f"Currently {state.state.replace('_', ' ')} outside{addr}. "
                f"Temperature is {temperature}° with {humidity}% humidity.")
    for state in hass.states.async_all("sensor"):
        eid = state.entity_id.lower()
        if (("outdoor" in eid or "outside" in eid)
                and state.attributes.get("device_class") == "temperature"):
            unit = state.attributes.get("unit_of_measurement", "°")
            return f"The outdoor temperature is {state.state}{unit}{addr}."
    return f"I don't have weather data available at the moment{addr}."


def _area_devices(hass, addr: str, asked: str):
    try:
        area_reg, ent_reg, dev_reg = _registries(hass)
        area = _first_area(area_reg, asked)
        if area is None:
            return f"I couldn't find an area matching '{asked}'{addr}."
        devices = []
        for entry in _area_members(hass, area.id, None, ent_reg, dev_reg):
            state = hass.states.get(entry.entity_id)
            if state:
                devices.append(f"{_name_of(state, entry.entity_id)} ({state.state})")
        if not devices:
            return f"No devices found in {area.name}{addr}."
        more = f" and {len(devices) - 10} more" if len(devices) > 10 else ""
        return (f"{area.name} has {_plural(len(devices), 'device')}: "
                f"{', '.join(devices[:10])}{more}.")
    except Exception:
        return None


def _ctx_query(hass, qtype, h="sir", area_match=""):
    """The answer to a question about the house, or None to carry on."""
    addr = _addr(h)
    if qtype == "indoor_temp":
        indoor = _indoor(hass, addr)
        if indoor:
            return indoor
        qtype = "weather"       # no thermostat: answer for outside
    simple = {"platform_status": _platform_status, "who_home": _who_home,
              "count_home": _count_home, "what_open": _what_open,
              "lights_on": _lights_on, "energy": _energy, "weather": _weather}
    if qtype in simple:
        return simple[qtype](hass, addr)
    if qtype == "person_home":
        return _person_home(hass, addr, area_match)
    if qtype == "area_devices" and area_match:
        return _area_devices(hass, addr, area_match)
    return None


# ── try_local, step by step ─────────────────────────────────────────────────

# A step's answer meaning "policy wants a confirmation: leave it to the agent".
_DEFER = object()


@dataclass
class _NotFound:
    """A command pattern matched but no device had that name."""
    name: str


def _repeat_last() -> LocalResult:
    """"Repeat that": the last thing Nova said, from spoken_history's memory
    (never the database; this runs on the event loop). Always handled, even
    with nothing to repeat, so it never reaches the agent."""
    from . import spoken_history
    last = spoken_history.get_last()
    if last is None:
        return LocalResult(text="I don't have a recent spoken message to repeat.",
                           success=False)
    return LocalResult(text=last["text"], success=True, repeat_of_id=last["id"])


def _answer_question(hass, normalized: str, honorific: str):
    for pattern, kind in _QUERY_PATTERNS:
        match = re.search(pattern, normalized)
        if not match:
            continue
        if kind == "repeat_last":
            return _repeat_last()
        answer = _ctx_query(hass, kind, honorific, match.group(1) if match.lastindex else "")
        if answer:
            _LOGGER.info("Local contextual: %s", kind)
            return LocalResult(text=answer, success=True)
    return None


async def _log_rows(hass, label: str, rows: list) -> dict:
    from . import action_log
    request_id = action_log.new_request_id()
    return await hass.async_add_executor_job(
        lambda: action_log.start_many(request_id, label, "chat", rows))


async def _act_on_each(hass, action: str, targets: list, row_ids: dict, device_id) -> int:
    """Send action to each (entity_id, ...) target, logging each row. The
    number sent."""
    sent = 0
    for target in targets:
        entity_id = target[0]
        row_id = row_ids.get(entity_id)
        ok = await _execute_action(hass, action, entity_id, {}, device_id=device_id)
        sent += ok
        if row_id is not None:
            if ok:
                await _logged(hass, row_id, "accepted")
            else:
                await _logged(hass, row_id, "failed", "service_call_failed")
    return sent


async def _bulk(hass, normalized: str, addr: str, device_id):
    """A command for a whole kind of device. None to carry on (no pattern,
    or nothing of that kind); _DEFER when policy wants a
    confirmation for the kind."""
    for pattern, action, domain, scope in _BULK_PATTERNS:
        match = re.search(pattern, normalized)
        if not match:
            continue
        area = match.group(1) if scope == "area" and match.lastindex else None
        if _needs_confirmation(hass, action, "", device_id):
            _LOGGER.info("Local: bulk '%s' needs confirmation — deferring to agent", action)
            return _DEFER
        if domain == "all":
            targets = [(s.entity_id, kind) for kind in ("light", "switch", "fan")
                       for s in hass.states.async_all(kind) if s.state == "on"]
            row_ids = await _log_rows(hass, "bulk_control", [
                {"key": eid, "domain": kind, "service": "turn_off", "entity_id": eid}
                for eid, kind in targets])
            done = await _act_on_each(hass, "turn_off", targets, row_ids, device_id)
            return LocalResult(text=f"Done{addr}. {_plural(done, 'device')} turned off.",
                               success=True)
        if area:
            targets = _find_entities_in_area(hass, area, domain)
        else:
            targets = [(s.entity_id, _name_of(s)) for s in hass.states.async_all(domain)]
        if not targets:
            continue
        row_ids = await _log_rows(hass, "bulk_control", [
            {"key": eid, "domain": domain, "entity_id": eid} for eid, _name in targets])
        done = await _act_on_each(hass, action, targets, row_ids, device_id)
        where = f" in {area}" if area else ""
        return LocalResult(
            text=f"Done{addr}. {_plural(done, domain)}{where} {action.replace('_', ' ')}.",
            success=done > 0)
    return None


async def _start_scene(hass, entity_id: str, kind: str, action_name: str, device_id):
    """Turn on a scene or script, logged as not checkable. Its contents are
    unknown here, so it could unlock a door as easily as dim a light: when
    policy would want a confirmation, nothing is sent and _DEFER comes back
    for the agent. Otherwise the error, or None when the call went out."""
    if _needs_confirmation_domain(hass, kind, "turn_on", entity_id, device_id):
        _LOGGER.info("Local: scene/script '%s' needs confirmation — deferring to agent",
                     entity_id)
        return _DEFER
    from . import action_log
    request_id = action_log.new_request_id()
    row_id = await hass.async_add_executor_job(lambda: action_log.start(
        request_id, action_name, "chat", domain=kind, service="turn_on",
        entity_id=entity_id))
    try:
        await hass.services.async_call(kind, "turn_on", {"entity_id": entity_id},
                                       blocking=True)
        await _logged(hass, row_id, "accepted")
        await hass.async_add_executor_job(lambda: action_log.mark_not_checkable(row_id))
    except Exception as exc:
        await _logged(hass, row_id, "failed", "service_call_failed")
        return exc
    return None


async def _scene(hass, normalized: str, addr: str, device_id):
    """"Activate X": a scene or script by name. _DEFER when policy wants a
    confirmation first."""
    for pattern, action, _hint in _INTENT_PATTERNS:
        if action != "scene":
            continue
        match = re.search(pattern, normalized)
        name = match.group(1) if match and match.lastindex else ""
        found = _find_scene_or_script(hass, name) if name else None
        if not found:
            continue
        entity_id, shown, kind = found
        error = await _start_scene(hass, entity_id, kind,
                                   "scene_activation" if kind == "scene" else "script_run",
                                   device_id)
        if error is _DEFER:
            return _DEFER
        if error is not None:
            return LocalResult(text=f"I wasn't able to activate {shown}{addr}. {error}",
                               success=False)
        _update_ctx(entity=entity_id, domain=kind)
        return LocalResult(text=f"Activating {shown} now{addr}.", success=True)
    return None


async def _goodnight_lights(hass, request_id: str) -> list:
    from . import action_log
    on = [s.entity_id for s in hass.states.async_all("light") if s.state == "on"]
    row_ids = {}
    if on:
        row_ids = await hass.async_add_executor_job(lambda: action_log.start_many(
            request_id, "goodnight_sweep", "chat",
            [{"key": e, "domain": "light", "service": "turn_off", "entity_id": e}
             for e in on]))
    sent = []
    for entity_id in on:
        try:
            await hass.services.async_call("light", "turn_off", {"entity_id": entity_id},
                                           blocking=True)
        except Exception:
            if row_ids.get(entity_id) is not None:
                await _logged(hass, row_ids[entity_id], "failed", "service_call_failed")
            continue
        sent.append(entity_id)
        if row_ids.get(entity_id) is not None:
            await _logged(hass, row_ids[entity_id], "accepted")
    for entity_id in sent:
        _background_check(hass, entity_id, "turn_off", "light", "turn_off",
                          row_ids.get(entity_id))
    return sent


async def _goodnight_locks(hass, request_id: str, device_id) -> tuple[list, list]:
    """Lock every unlocked lock lockdown covers. Each still goes through the
    one authority check (8.24.0). A lock the live check holds back (8.46.0:
    its door is open, or it cannot be read) is mentioned, never skipped
    silently, and an open door can still be locked on request."""
    from . import action_log, policy
    unlocked = [s.entity_id for s in _unlocked_locks(hass)]
    row_ids = {}
    if unlocked:
        row_ids = await hass.async_add_executor_job(lambda: action_log.start_many(
            request_id, "goodnight_sweep", "chat",
            [{"key": e, "domain": "lock", "service": "lock", "entity_id": e}
             for e in unlocked]))
    sent, notes = [], []
    for entity_id in unlocked:
        row_id = row_ids.get(entity_id)
        verdict = policy.authorize_now(hass, policy.AuthorityRequest(
            "lock", "lock", entity_id, device_id=device_id or ""))
        if not verdict.allowed:
            if verdict.code and not verdict.already_secure:
                tail = (" Ask me to lock it if you still want it locked."
                        if verdict.approval == "deferred" else "")
                notes.append(verdict.note + tail)
            if row_id is not None:
                await _logged(hass, row_id, "blocked",
                              verdict.code or "confirmation_not_approved")
            continue
        try:
            await hass.services.async_call("lock", "lock", {"entity_id": entity_id},
                                           blocking=True)
        except Exception:
            if row_id is not None:
                await _logged(hass, row_id, "failed", "service_call_failed")
            continue
        sent.append(entity_id)
        if row_id is not None:
            await _logged(hass, row_id, "accepted")
    for entity_id in sent:
        _background_check(hass, entity_id, "lock", "lock", "lock", row_ids.get(entity_id))
    return sent, notes


async def _goodnight(hass, addr: str, device_id):
    """"Goodnight": a goodnight scene when there is one, otherwise turn off
    the lights and lock up. The reply says what was sent, not that it is
    done; each device is then checked in the background."""
    found = (_find_scene_or_script(hass, "goodnight")
             or _find_scene_or_script(hass, "good night"))
    if found:
        entity_id, shown, kind = found
        started = await _start_scene(hass, entity_id, kind, "scene_activation", device_id)
        if started is _DEFER:
            return _DEFER
        if started is None:
            return LocalResult(text=f"Goodnight{addr}. I've triggered {shown}. Rest well.",
                               success=True)
    from . import action_log
    request_id = action_log.new_request_id()
    lights = await _goodnight_lights(hass, request_id)
    locks, notes = await _goodnight_locks(hass, request_id, device_id)
    return LocalResult(
        text=(f"Goodnight{addr}. I've sent the command to turn off "
              f"{_plural(len(lights), 'light')} and lock {_plural(len(locks), 'lock')}. "
              + "".join(note + " " for note in notes) + "Rest well."),
        success=True)


def _action_args(action: str, groups: tuple) -> dict:
    """Numbers spoken with the command: a brightness, a temperature or a
    volume."""
    extra = groups[1] if len(groups) > 1 else None
    if action == "dim":
        return {"brightness_pct": int(extra)} if extra else {}
    keys = {"set_temp": "temperature", "set_temp_named": "temperature",
            "volume_set": "volume_level"}
    if action not in keys:
        return {}
    spoken = extra if (extra and action != "volume_set") else (groups[0] if groups else None)
    try:
        return {keys[action]: int(spoken)}
    except (TypeError, ValueError):
        return {}


async def _single(hass, normalized: str, honorific: str, aliases: dict, device_id):
    """A command or question about one device: a LocalResult, _DEFER, a
    _NotFound for a name no device has, or None when nothing matched."""
    addr = _addr(honorific)
    missing = None
    for pattern, action, hint in _DEVICE_PATTERNS:
        match = re.search(pattern, normalized)
        if match is None:
            continue
        _LOGGER.info("Local intent: action=%s", action)
        if action in _FIXED_REPLIES:
            return LocalResult(text=_query_resp(hass, action, "", "", honorific),
                               success=True)
        groups = match.groups()
        name = groups[0] if groups else ""
        if not name:
            continue
        found = (_find_entity(hass, name, hint, aliases)
                 or _find_entity(hass, name, None, aliases))
        if isinstance(found, _AmbiguousEntity):
            return LocalResult(text=_build_local_clarification(found.candidates, addr),
                               success=False)
        if not found:
            missing = name
            _LOGGER.info("Local: pattern matched '%s' but entity '%s' not found "
                         "(domain=%s), trying next pattern...", action, name, hint)
            continue
        entity_id, shown = found
        if action.startswith("query_"):
            reply = _query_resp(hass, action, entity_id, shown, honorific)
            if reply:
                return LocalResult(text=reply, success=True)
            continue
        if _needs_confirmation(hass, action, entity_id, device_id):
            _LOGGER.info("Local: '%s' on %s needs confirmation — deferring to agent",
                         action, entity_id)
            return _DEFER
        args = _action_args(action, groups)
        status = await _execute_action_verified(hass, action, entity_id, args, shown,
                                                device_id=device_id)
        ok = status != "error"
        _update_ctx(entity=entity_id, domain=entity_id.split(".")[0], action=action)
        return LocalResult(text=_resp(action, shown, ok, args, honorific, status=status),
                           success=ok)
    return None if missing is None else _NotFound(missing)


def _appliance_answer(addr: str) -> LocalResult:
    try:
        from . import appliance_monitor
        if not appliance_monitor.is_running():
            text = (f"The appliance monitor isn't running at the moment{addr}. It starts "
                    "automatically with the observer. Check if the observer is enabled.")
            return LocalResult(text=text, success=True)
        watching = [f"{s['friendly_name']} ({s['phase']})"
                    for s in appliance_monitor.status().get("sensors", {}).values()]
    except Exception:
        return LocalResult(text=(f"I'll keep an eye on it{addr}. If I have a power sensor "
                                 "for that appliance, I'll announce when the cycle finishes."),
                           success=True)
    if watching:
        return LocalResult(text=(f"Already on it{addr}. I'm monitoring {', '.join(watching)}. "
                                 "I'll announce when the cycle completes."), success=True)
    return LocalResult(text=(f"I'm monitoring for appliance cycles{addr}, but I haven't found "
                             "any power sensors matching your appliances yet. Make sure the "
                             "power monitoring sensor has 'washer', 'dryer', or 'dishwasher' "
                             "in its name."), success=True)


def _bare_name(hass, normalized: str, addr: str, aliases: dict):
    """A short phrase that is just a device name reads its state."""
    found = _find_entity(hass, normalized, None, aliases)
    if isinstance(found, _AmbiguousEntity):
        return LocalResult(text=_build_local_clarification(found.candidates, addr),
                           success=False)
    if found:
        entity_id, shown = found
        state = hass.states.get(entity_id)
        if state:
            return LocalResult(text=f"{shown} is currently {state.state}{addr}.",
                               success=True)
    return None


async def try_local(hass, text, honorific="sir", force=False, device_id=None):
    """Answer the request here, or return None for the agent.

    force means the agent cannot be reached. It changes only one reply: a
    device that cannot be found is said so plainly instead of returning
    None. What is handled is otherwise the same online and offline (8.49.0).

    device_id is the requesting device (conversation.py passes the voice
    satellite's). It goes to every policy check, so a spoken unlock or open
    defers to the agent's guarded path, and offline does not run at all."""
    addr = _addr(honorific)
    normalized = _normalize(text)
    complexity = score_complexity(text)
    if complexity >= _AGENT_SCORE:
        _LOGGER.debug("Local: complexity %d for '%s' — LLM", complexity, text[:60])
        return None

    aliases = await hass.async_add_executor_job(_read_aliases)

    answer = _answer_question(hass, normalized, honorific)
    if answer is not None:
        return answer
    if _READ_ONLY_RE.search(normalized):
        _LOGGER.debug("Local: read-only request, no local action")
        return None

    for step in (_bulk(hass, normalized, addr, device_id),
                 _scene(hass, normalized, addr, device_id)):
        outcome = await step
        if outcome is _DEFER:
            return None
        if outcome is not None:
            return outcome

    if re.search(_GOODNIGHT_RE, normalized):
        outcome = await _goodnight(hass, addr, device_id)
        return None if outcome is _DEFER else outcome

    outcome = await _single(hass, normalized, honorific, aliases, device_id)
    if outcome is _DEFER:
        return None
    if isinstance(outcome, _NotFound):
        missing = outcome.name
        if force:
            return LocalResult(
                text=(f"I couldn't find a device matching '{missing}'{addr}, and I'm "
                      "offline so I can't do a deeper search right now. Try the exact "
                      "device name."),
                success=False)
        # The agent's search is better at finding devices than this one.
        _LOGGER.info("Local: entity '%s' not found — escalating to agentic LLM "
                     "with search_entities", missing)
        return None
    if outcome is not None:
        return outcome

    if re.search(_APPLIANCE_RE, normalized):
        return _appliance_answer(addr)

    if _ctx_fresh() and re.search(_FOLLOW_UP_RE, normalized):
        result = await try_local(hass, re.sub(_FOLLOW_UP_RE, "", normalized), honorific,
                                 force=force, device_id=device_id)
        if result and result.handled:
            return result

    if complexity < 40 and _looks_like_entity_name(normalized):
        bare = _bare_name(hass, normalized, addr, aliases)
        if bare is not None:
            return bare

    _LOGGER.debug("Local: no match for '%s' (complexity=%d) — LLM", text[:60], complexity)
    return None


_NOT_A_NAME_START = ("what", "who", "when", "where", "why", "how", "is", "are", "can",
                     "could", "would", "should", "do", "does", "did", "will", "tell",
                     "explain", "describe", "give", "show me how", "help")


def _looks_like_entity_name(text: str) -> bool:
    """Whether a short phrase could be a bare device name rather than a
    question or a sentence, so chat never reaches the device search: four
    words at most, no "?", not opening like a question, and not about Nova
    ("you", "your")."""
    words = text.strip().lower().split()
    if not words or text.strip().endswith("?") or len(words) > 4:
        return False
    if words[0] in _NOT_A_NAME_START:
        return False
    return "you" not in words and "your" not in words
