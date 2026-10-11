"""Voice phrases (8.32.0): things a person says, linked to an action.

"Nova, the house is cold" runs a script, an automation, a scene or a shell
command the person set up in the panel. This module is the pure part: it
normalises text, matches it against the saved phrases and checks a saved
entry. It uses only the standard library, so the tests run without Home
Assistant. voice_phrase_runner.py does the running.

Matching is local and word based. It tolerates small wording and
speech-to-text slips ("the house is a bit cold", "im cold") but has guards
for the near misses that matter: the opposite word ("turn off" against
"turn on"), a negation ("the house is not cold"), a question ("is the house
cold") and a long sentence that only happens to contain the phrase.
"""
from __future__ import annotations

import difflib
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Optional

CONFIG_KEY = "voice_phrases"
MAX_ENTRIES = 100
MAX_PHRASES = 10
MAX_TARGETS = 10
MAX_DATA_BYTES = 2048
MAX_REPLY = 200
MAX_NAME = 60
MAX_PHRASE = 80
MIN_PHRASE = 2
MAX_PROMPT_ENTRIES = 50

SHELL_DOMAIN = "shell_command"
MATCH_MODES = ("flexible", "exact")
# Keys that would aim a call somewhere other than its checked target list.
_FORBIDDEN_DATA_KEYS = {"entity_id", "area_id", "device_id", "target"}

_STOP_WORDS = frozenset(
    "the a an in here it is am are bit really so very just now my our".split())
_NEGATIONS = frozenset({"not", "no", "never", "cannot", "nothing"})
_QUESTION_STARTS = frozenset(
    "is are was were what why how when who does do did should could would will".split())
_OPPOSITES = (
    ("on", "off"), ("up", "down"), ("open", "close"), ("lock", "unlock"),
    ("start", "stop"), ("arm", "disarm"), ("enable", "disable"),
    ("warm", "cold"), ("hot", "cold"), ("warmer", "colder"), ("high", "low"),
    ("more", "less"), ("increase", "decrease"), ("raise", "lower"),
)
_OPPOSITE_OF: dict[str, set[str]] = {}
for _a, _b in _OPPOSITES:
    _OPPOSITE_OF.setdefault(_a, set()).add(_b)
    _OPPOSITE_OF.setdefault(_b, set()).add(_a)

_CONTRACTIONS = (
    (re.compile(r"\bi'?m\b"), "i am"),
    (re.compile(r"\bit'?s\b"), "it is"),
    (re.compile(r"\bthat'?s\b"), "that is"),
    (re.compile(r"\bi'?ve\b"), "i have"),
    (re.compile(r"\bcan'?t\b"), "cannot"),
    (re.compile(r"\bwon'?t\b"), "will not"),
)
# Any word ending in n't, with the apostrophe.
_NT_APOSTROPHE = re.compile(r"\b(\w+)n't\b")
# Without the apostrophe only these, so "want" or "plant" stay as they are.
_NT_BARE = {
    "isnt": "is not", "doesnt": "does not", "arent": "are not",
    "wasnt": "was not", "werent": "were not", "dont": "do not",
    "didnt": "did not", "hasnt": "has not", "havent": "have not",
    "shouldnt": "should not", "wouldnt": "would not", "couldnt": "could not",
}
_NT_BARE_RE = re.compile(r"\b(" + "|".join(_NT_BARE) + r")\b")

_LEADING_ADDRESS = ("hey nova", "ok nova", "okay nova", "hi nova", "nova")
_TRAILING = ("nova", "please")
_LEADING_POLITE = ("please", "can you", "could you", "would you")

_SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_PHRASE_ID_RE = re.compile(r"^vp_[0-9a-f]{8}$")


@dataclass(frozen=True)
class PhraseMatch:
    phrase_id: str
    name: str
    phrase: str
    tier: str            # "exact" or "flexible"


# ── normalising ─────────────────────────────────────────────────────────────

def _strip_prefixes(text: str, prefixes) -> str:
    changed = True
    while changed and text:
        changed = False
        for p in prefixes:
            if text == p:
                return ""
            if text.startswith(p + " "):
                text = text[len(p) + 1:]
                changed = True
                break
    return text


def _strip_suffixes(text: str, suffixes) -> str:
    changed = True
    while changed and text:
        changed = False
        for s in suffixes:
            if text == s:
                return ""
            if text.endswith(" " + s):
                text = text[:-(len(s) + 1)]
                changed = True
                break
    return text


def normalise(text: Any) -> str:
    """Lowercase, expand contractions, drop punctuation, the wake word and
    leading politeness. Never raises."""
    if not isinstance(text, str):
        return ""
    t = text.lower()
    for q in ("‘", "’", "ʼ", "`", "´"):
        t = t.replace(q, "'")
    for pattern, repl in _CONTRACTIONS:
        t = pattern.sub(repl, t)
    t = _NT_APOSTROPHE.sub(r"\1 not", t)
    t = _NT_BARE_RE.sub(lambda m: _NT_BARE[m.group(1)], t)
    t = t.replace("'", "")
    t = re.sub(r"[^\w\s]|_", " ", t)
    t = " ".join(t.split())
    t = _strip_prefixes(t, _LEADING_ADDRESS)
    t = _strip_suffixes(t, _TRAILING)
    t = _strip_prefixes(t, _LEADING_POLITE)
    return t


def _content(words: list[str]) -> list[str]:
    return [w for w in words if w not in _STOP_WORDS]


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    # A slip of one letter or so in a longer word. Same first letter, and
    # only from five letters up, so "cold" never matches "old", "lock"
    # never matches "unlock" and "heating" never matches "seating".
    if len(a) < 5 or len(b) < 5 or a[0] != b[0]:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


# ── matching one phrase ─────────────────────────────────────────────────────

def _guards_pass(u_words: list[str], p_words: list[str]) -> bool:
    u_set, p_set = set(u_words), set(p_words)
    # Polarity: "turn off the heating" is not "turn on the heating".
    for w in u_set:
        for opp in _OPPOSITE_OF.get(w, ()):
            if opp in p_set and w not in p_set:
                return False
    # Negation: "the house is not cold" is not "the house is cold".
    if bool(u_set & _NEGATIONS) != bool(p_set & _NEGATIONS):
        return False
    # Question: "is the house cold" asks, it does not tell.
    if u_words and u_words[0] in _QUESTION_STARTS and not (
            p_words and p_words[0] in _QUESTION_STARTS):
        return False
    return True


def _match_phrase(u_norm: str, phrase: str, mode: str) -> Optional[tuple[str, int, int]]:
    """(tier, phrase content words, extra utterance words) or None."""
    p_norm = normalise(phrase)
    if not p_norm:
        return None
    if u_norm == p_norm:
        return ("exact", len(_content(p_norm.split())), 0)
    if mode != "flexible":
        return None
    u_words, p_words = u_norm.split(), p_norm.split()
    u_content, p_content = _content(u_words), _content(p_words)
    if not p_content:
        return None
    used: set[int] = set()
    for pw in p_content:
        hit = next((i for i, uw in enumerate(u_content)
                    if i not in used and _same_word(pw, uw)), None)
        if hit is None:
            return None
        used.add(hit)
    if not _guards_pass(u_words, p_words):
        return None
    extra = len(u_content) - len(used)
    if extra > min(3, len(p_content)):
        return None
    return ("flexible", len(p_content), extra)


def _entries(config: Any) -> list[dict]:
    if not isinstance(config, dict) or not config.get("enabled", True):
        return []
    entries = config.get("entries")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("enabled", True)
            and isinstance(e.get("phrase_id"), str)]


def match(text: Any, config: Any) -> Optional[PhraseMatch]:
    """The one entry these words mean, or None. Never raises.

    An exact match wins. Otherwise the entry whose phrase has the most
    content words, then the fewest extra words in what was said. Two
    different entries still tied is ambiguous, so nothing matches."""
    try:
        u_norm = normalise(text)
        if not u_norm:
            return None
        found = []   # (exact?, content words, -extra, entry, phrase, tier)
        for entry in _entries(config):
            mode = entry.get("match") if entry.get("match") in MATCH_MODES else "flexible"
            for phrase in entry.get("phrases") or []:
                if not isinstance(phrase, str):
                    continue
                hit = _match_phrase(u_norm, phrase, mode)
                if hit is not None:
                    tier, n_content, extra = hit
                    found.append(((tier == "exact", n_content, -extra), entry, phrase, tier))
        if not found:
            return None
        found.sort(key=lambda f: f[0], reverse=True)
        best_rank, best_entry, best_phrase, best_tier = found[0]
        for rank, entry, _p, _t in found[1:]:
            if rank != best_rank:
                break
            if entry.get("phrase_id") != best_entry.get("phrase_id"):
                return None      # two different entries tie: ambiguous
        return PhraseMatch(best_entry["phrase_id"], str(best_entry.get("name") or ""),
                           best_phrase, best_tier)
    except Exception:
        return None


# ── checking a saved entry ──────────────────────────────────────────────────

def new_phrase_id() -> str:
    return "vp_" + secrets.token_hex(4)


def _walk_data(value: Any, errors: list[str], depth: int = 0) -> None:
    if depth > 20:
        errors.append("The extra data is nested too deeply.")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                errors.append("Every key in the extra data must be text.")
                continue
            if k in _FORBIDDEN_DATA_KEYS:
                errors.append(f"The extra data may not set '{k}'. Pick the targets instead.")
            if "{{" in k or "{%" in k:
                errors.append("The extra data may not contain templates.")
            _walk_data(v, errors, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _walk_data(v, errors, depth + 1)
    elif isinstance(value, str):
        if "{{" in value or "{%" in value:
            errors.append("The extra data may not contain templates.")


def validate_entry(entry: Any, existing: Any, *, allowed_domains) -> tuple[Optional[dict], list[str], list[str]]:
    """Check one entry from the panel. Returns (clean entry, errors,
    warnings); the clean entry is None when there are errors. The parts
    that need Home Assistant (does the service exist, do the entities
    exist) are the caller's."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(entry, dict):
        return None, ["The phrase must be an object."], []
    current = [e for e in (existing if isinstance(existing, list) else [])
               if isinstance(e, dict)]

    # phrase_id: the backend makes it; one sent in means "update this one".
    pid = entry.get("phrase_id")
    if pid in (None, ""):
        if len(current) >= MAX_ENTRIES:
            errors.append(f"You can have at most {MAX_ENTRIES} voice phrases.")
        taken = {e.get("phrase_id") for e in current}
        pid = new_phrase_id()
        while pid in taken:
            pid = new_phrase_id()
    elif not (isinstance(pid, str) and _PHRASE_ID_RE.match(pid)
              and any(e.get("phrase_id") == pid for e in current)):
        errors.append("That voice phrase no longer exists.")
        pid = None

    name = entry.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= MAX_NAME:
        errors.append(f"The name must be 1 to {MAX_NAME} characters.")

    # Phrases, and no wording another entry already owns.
    others = {}
    for e in current:
        if e.get("phrase_id") == pid:
            continue
        for p in e.get("phrases") or []:
            if isinstance(p, str):
                others[normalise(p)] = str(e.get("name") or "")
    raw_phrases = entry.get("phrases")
    phrases: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw_phrases, list):
        raw_phrases = []
    if len(raw_phrases) > MAX_PHRASES:
        errors.append(f"Add 1 to {MAX_PHRASES} phrases.")
        raw_phrases = []
    phrase_errors = len(errors)
    for p in raw_phrases:
        if not isinstance(p, str):
            errors.append("Every phrase must be text.")
            continue
        text = " ".join(p.split())
        if not text:
            continue
        norm = normalise(text)
        if len(text) > MAX_PHRASE or not MIN_PHRASE <= len(norm) <= MAX_PHRASE:
            errors.append(f"\"{text[:MAX_PHRASE]}\" must be {MIN_PHRASE} to {MAX_PHRASE} characters.")
            continue
        if not _content(norm.split()):
            errors.append(f"\"{text}\" needs at least one real word, not only small words like \"the\" or \"it\".")
            continue
        if norm in seen:
            continue
        if norm in others:
            errors.append(f"\"{text}\" is already used by \"{others[norm]}\".")
            continue
        seen.add(norm)
        phrases.append(text)
    # None left, and no message yet saying why.
    if not phrases and len(errors) == phrase_errors:
        errors.append(f"Add 1 to {MAX_PHRASES} phrases.")

    mode = entry.get("match", "flexible")
    if mode not in MATCH_MODES:
        errors.append("Match must be flexible or exact.")

    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append("Enabled must be on or off.")
    allow_ai = entry.get("allow_ai", False)
    if not isinstance(allow_ai, bool):
        errors.append("\"Let the AI pick this\" must be on or off.")
        allow_ai = False

    # The action.
    action = entry.get("action")
    clean_action: dict = {}
    domain = ""
    if not isinstance(action, dict):
        errors.append("Pick what to run.")
    else:
        unknown = set(action) - {"service", "target", "data"}
        if unknown:
            errors.append("The action may only have a service, targets and data.")
        service = action.get("service")
        if not (isinstance(service, str) and _SERVICE_RE.match(service)):
            errors.append("The service must look like domain.service.")
        else:
            domain = service.split(".", 1)[0]
            if domain != SHELL_DOMAIN and domain not in allowed_domains:
                errors.append(f"Nova can't run {domain} services from a voice phrase.")
            clean_action["service"] = service

        target = action.get("target")
        if target not in (None, {}):
            if not isinstance(target, dict) or set(target) != {"entity_id"}:
                errors.append("Targets may only be entity ids.")
            else:
                ids = target["entity_id"]
                id_list = [ids] if isinstance(ids, str) else ids
                if not (isinstance(id_list, list) and id_list
                        and all(isinstance(i, str) for i in id_list)):
                    errors.append("Targets may only be entity ids.")
                elif len(id_list) > MAX_TARGETS:
                    errors.append(f"Pick at most {MAX_TARGETS} targets.")
                elif domain == SHELL_DOMAIN:
                    errors.append("A shell command has no targets.")
                else:
                    for i in id_list:
                        if not _ENTITY_RE.match(i):
                            errors.append(f"\"{i}\" is not an entity id.")
                        elif domain and i.split(".", 1)[0] != domain:
                            errors.append(f"{i} can't be used with a {domain} service.")
                    if len(set(id_list)) != len(id_list):
                        errors.append("A target is listed twice.")
                    clean_action["target"] = {"entity_id": ids}

        data = action.get("data")
        if data not in (None, {}):
            if not isinstance(data, dict):
                errors.append("The extra data must be a JSON object.")
            else:
                try:
                    size = len(json.dumps(data, separators=(",", ":")).encode("utf-8"))
                except (TypeError, ValueError):
                    size = MAX_DATA_BYTES + 1
                    errors.append("The extra data must be plain JSON.")
                if size > MAX_DATA_BYTES:
                    errors.append(f"The extra data must be at most {MAX_DATA_BYTES} bytes.")
                found: list[str] = []
                _walk_data(data, found)
                for msg in found:
                    if msg not in errors:
                        errors.append(msg)
                clean_action["data"] = data
        clean_action.setdefault("data", {})

    reply = entry.get("reply", "")
    if reply is None:
        reply = ""
    if not isinstance(reply, str):
        errors.append("The reply must be text.")
        reply = ""
    reply = reply.strip()
    if len(reply) > MAX_REPLY:
        errors.append(f"The reply must be at most {MAX_REPLY} characters.")

    if errors:
        return None, list(dict.fromkeys(errors)), warnings
    return {
        "phrase_id": pid,
        "name": name,
        "phrases": phrases,
        "match": mode,
        # Never for a shell command: the AI may not choose to run one.
        "allow_ai": bool(allow_ai) and domain != SHELL_DOMAIN,
        "enabled": enabled,
        "action": clean_action,
        "reply": reply,
    }, [], warnings


def target_ids(entry: dict) -> list[str]:
    """The entity ids a saved entry targets, as a list."""
    target = ((entry.get("action") or {}).get("target") or {}).get("entity_id")
    if isinstance(target, str):
        return [target]
    if isinstance(target, list):
        return [t for t in target if isinstance(t, str)]
    return []


def find_entry(config: Any, phrase_id: Any) -> Optional[dict]:
    if not isinstance(config, dict) or not isinstance(phrase_id, str):
        return None
    for e in config.get("entries") or []:
        if isinstance(e, dict) and e.get("phrase_id") == phrase_id:
            return e
    return None


def ai_entries(config: Any) -> list[dict]:
    """Enabled entries the AI may pick by meaning: allow_ai on, never a
    shell command, at most MAX_PROMPT_ENTRIES."""
    out = []
    for e in _entries(config):
        service = str((e.get("action") or {}).get("service") or "")
        if e.get("allow_ai") is True and not service.startswith(SHELL_DOMAIN + "."):
            out.append(e)
    return out[:MAX_PROMPT_ENTRIES]


def prompt_lines(config: Any) -> tuple[str, list[str]]:
    """The phrases the AI may pick, for the prompt: numbered lines holding
    only the user's own text, "[1] name (e.g. "first phrase")", and the
    phrase ids in the same order. The lines go inside a prompt fence and the
    ids outside it. ("", []) when there are none."""
    lines, ids = [], []
    for i, e in enumerate(ai_entries(config), start=1):
        phrases = [p for p in e.get("phrases") or [] if isinstance(p, str)]
        name = " ".join(str(e.get("name") or "").split())
        first = " ".join(phrases[0].split()) if phrases else ""
        lines.append(f'[{i}] {name} (e.g. "{first}")')
        ids.append(str(e["phrase_id"]))
    return "\n".join(lines), ids
