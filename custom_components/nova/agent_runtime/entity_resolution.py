"""Deterministic entity resolution: from what a person asked for to entity ids.

Pure: it works on an immutable list of candidates the caller read from Home
Assistant (entity_id, domain, friendly name, state, area) and never touches
Home Assistant, a service or storage. The entity_id stays the identity; the
friendly name and area are only matched against and shown.

Matching order, first stage that matches wins:
  1. an explicit entity_id;
  2. an exact friendly name (or learned alias, resolved by the caller) after
     normalization;
  3. an exact normalized object name ("porch_light" for light.porch_light);
  4. the explicit domain the caller asked for — every stage is restricted
     to it, and nothing below can override it;
  5. a domain the request itself names with a plural noun ("lights",
     "locks", "switches"), optionally with a state ("lights that are on") and
     a remaining name or area ("kitchen lights");
  6. bounded fuzzy scoring, only after the deterministic stages. A unique
     fuzzy pick needs a minimum score and a clear margin over the next
     candidate; otherwise a request for one target is a clarification.

Ties sort by entity_id, never by set or dictionary order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

DEFAULT_DOMAINS = (
    "light", "switch", "lock", "cover", "climate", "fan",
    "media_player", "sensor", "binary_sensor", "scene",
    "script", "automation", "person",
)

# Plural nouns that name a whole Home Assistant domain. Singular words are
# deliberately absent: "porch light" is often a switch, and "office light"
# must stay a name to match (and to stay ambiguous when it is).
PLURAL_DOMAINS = {
    "lights": "light", "lamps": "light",
    "switches": "switch", "plugs": "switch",
    "locks": "lock",
    "fans": "fan",
    "covers": "cover", "blinds": "cover", "shades": "cover", "curtains": "cover",
    "thermostats": "climate",
    "scenes": "scene", "scripts": "script", "automations": "automation",
    "people": "person",
}

# State words a plural request may filter on, per domain.
STATE_WORDS = {
    "light": {"on": "on", "off": "off"},
    "switch": {"on": "on", "off": "off"},
    "fan": {"on": "on", "off": "off"},
    "lock": {"locked": "locked", "unlocked": "unlocked"},
    "cover": {"open": "open", "closed": "closed", "shut": "closed"},
    "person": {"home": "home", "away": "not_home"},
}

FILLER = frozenset({
    "the", "all", "my", "our", "that", "which", "what", "are", "is", "currently", "now",
    "still", "turned", "right", "every", "any", "list", "show", "me", "tell", "of", "in",
    "a", "an", "whichever", "please",
})

MIN_SCORE = 25.0            # below this a fuzzy candidate is not returned at all
MIN_UNIQUE_SCORE = 40.0     # a unique fuzzy pick needs at least this
TIE_RATIO = 0.85            # runner-up/top at or above this is a real tie
MAX_RESULTS = 15
MAX_LISTED = 30

_EID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class Candidate:
    entity_id: str
    domain: str
    friendly_name: str
    state: str
    area: str = ""


@dataclass(frozen=True, slots=True)
class Match:
    entity_id: str
    friendly_name: str
    state: str
    score: float
    matched_by: str
    area: str = ""

    def as_dict(self) -> dict:
        out = {"entity_id": self.entity_id, "friendly_name": self.friendly_name,
               "state": self.state, "score": self.score, "matched_by": self.matched_by}
        if self.area:
            out["area"] = self.area       # shown so a clarification can tell rooms apart
        return out


@dataclass(frozen=True, slots=True)
class Resolution:
    matches: tuple[Match, ...]
    ambiguous: bool = False
    stage: str = ""

    def as_payload(self):
        """The search_entities JSON payload shape."""
        if self.ambiguous:
            return {"ambiguous": True, "candidates": [m.as_dict() for m in self.matches]}
        return [m.as_dict() for m in self.matches]


def normalize(text: str) -> str:
    t = str(text or "").casefold().replace("_", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def _bigrams(s):
    return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else {s}


def fuzzy(a: str, b: str) -> float:
    """Bigram Dice / containment similarity, 0-100 (the scorer search_entities
    has always used)."""
    if a == b:
        return 100.0
    if not a or not b:
        return 0.0
    bg_a, bg_b = _bigrams(a), _bigrams(b)
    overlap = len(bg_a & bg_b)
    dice = (2.0 * overlap) / (len(bg_a) + len(bg_b)) * 100 if bg_a and bg_b else 0
    contain = len(a) / len(b) * 80 if a in b else (len(b) / len(a) * 80 if b in a else 0)
    return max(dice, contain)


def name_score(query: str, cand: Candidate) -> float:
    """The long-standing name score of one candidate against a query."""
    fname = (cand.friendly_name or "").lower()
    eid = cand.entity_id.lower()
    words = set(query.split())
    if query == fname:
        return 100.0
    if query in fname:
        return 80.0
    if query.replace(" ", "_") in eid:
        return 70.0
    if words and words.issubset(set(fname.split())):
        return 65.0
    score = 0.0
    fz = fuzzy(query, fname)
    if fz > 45:
        score = fz * 0.7
    if not score and words:
        fname_words = set(fname.split())
        hits = 0
        for qw in words:
            for fw in fname_words:
                if fuzzy(qw, fw) > 60:
                    hits += 1
                    break
        if hits:
            score = (hits / len(words)) * 50
    return score


def _match(c: Candidate, score: float, how: str) -> Match:
    return Match(c.entity_id, c.friendly_name, c.state, round(float(score), 1), how, c.area)


def _sorted(matches: Iterable[Match]) -> list[Match]:
    return sorted(matches, key=lambda m: (-m.score, m.entity_id))


def _listing(cands: Iterable[Candidate], how: str) -> list[Match]:
    ordered = sorted(cands, key=lambda c: (normalize(c.friendly_name) or c.entity_id,
                                           c.entity_id))
    return [_match(c, 100.0, how) for c in ordered][:MAX_LISTED]


def domain_intent(query: str, allowed: Optional[set] = None):
    """(domains, state, residual) when the request names whole domains with
    plural nouns, else None. `allowed` is the explicit domain constraint:
    a plural that names another domain is left as an ordinary word."""
    words = normalize(query).split()
    domains: list[str] = []
    rest: list[str] = []
    for w in words:
        dom = PLURAL_DOMAINS.get(w)
        if dom and (allowed is None or dom in allowed):
            if dom not in domains:
                domains.append(dom)
        else:
            rest.append(w)
    if not domains:
        return None
    state = None
    residual = []
    for w in rest:
        hit = next((STATE_WORDS.get(d, {}).get(w) for d in domains
                    if w in STATE_WORDS.get(d, {})), None)
        if hit is not None and state is None:
            state = hit
        elif w not in FILLER:
            residual.append(w)
    return tuple(domains), state, " ".join(residual)


def resolve(query: str, candidates: Iterable[Candidate], *, domain: Optional[str] = None,
            require_unique: bool = False) -> Resolution:
    q = str(query or "").strip().lower()
    pool = [c for c in candidates if not domain or c.domain == domain]
    pool.sort(key=lambda c: c.entity_id)

    # 1. explicit entity_id
    if _EID_RE.match(q):
        hit = [c for c in pool if c.entity_id == q]
        if hit:
            return Resolution((_match(hit[0], 100.0, "entity_id"),), stage="entity_id")

    nq = normalize(q)
    # 2. exact friendly name, then 3. exact object name. Resolving one
    # target, the exact stage decides alone; browsing, exact matches lead and
    # the scored matches follow.
    obj = nq.replace(" ", "_")
    for stage, exact in (
            ("friendly_name", [c for c in pool if nq and normalize(c.friendly_name) == nq]),
            ("object_name", [c for c in pool if obj and c.entity_id.split(".", 1)[-1] == obj])):
        if not exact:
            continue
        leading = [_match(c, 100.0, stage) for c in exact]
        if require_unique:
            return _finish(leading, require_unique, stage, exact_stage=True)
        ids = {c.entity_id for c in exact}
        rest = [m for m in _fuzzy_matches(q, nq, pool) if m.entity_id not in ids]
        return Resolution(tuple((leading + rest)[:MAX_RESULTS]), stage=stage)

    # 4 + 5. a domain named by the request (within any explicit domain)
    intent = domain_intent(q, {domain} if domain else None)
    if intent is not None:
        domains, state, residual = intent
        scoped = [c for c in pool if c.domain in domains
                  and (state is None or str(c.state).lower() == state)]
        how = "domain: " + ", ".join(domains) + (f" ({state})" if state else "")
        if not residual:
            return Resolution(tuple(_listing(scoped, how)), stage="domain")
        area_hits = [c for c in scoped if c.area and normalize(c.area) == residual]
        if area_hits:
            return Resolution(tuple(_listing(area_hits, how + f", area: {residual}")),
                              stage="domain_area")
        scored = [_match(c, s, how) for c in scoped
                  if (s := name_score(residual, c)) > MIN_SCORE]
        return _finish(_sorted(scored), require_unique, "domain_name")

    # 6. bounded fuzzy matching over names (and areas)
    return _finish(_fuzzy_matches(q, nq, pool), require_unique, "fuzzy")


def _fuzzy_matches(q: str, nq: str, pool: list) -> list[Match]:
    scored = []
    for c in pool:
        s = name_score(q, c)
        how = "name"
        if c.area and normalize(c.area) == nq and s < 75.0:
            s, how = 75.0, "area"
        if s > MIN_SCORE:
            scored.append(_match(c, s, how))
    return _sorted(scored)


def _finish(matches: list[Match], require_unique: bool, stage: str,
            exact_stage: bool = False) -> Resolution:
    matches = matches[:MAX_RESULTS]
    if not require_unique or len(matches) < 2:
        return Resolution(tuple(matches), stage=stage)
    if exact_stage:
        # Several entities share the exact name: a real ambiguity.
        return Resolution(tuple(matches[:4]), ambiguous=True, stage=stage)
    top = matches[0].score
    if top <= 0:
        return Resolution(tuple(matches), stage=stage)
    if matches[1].score / top >= TIE_RATIO:
        tied = [m for m in matches if m.score / top >= TIE_RATIO][:4]
        return Resolution(tuple(tied), ambiguous=True, stage=stage)
    if top < MIN_UNIQUE_SCORE:
        # Nothing clears the bar for acting on one target: ask.
        return Resolution(tuple(matches[:4]), ambiguous=True, stage=stage)
    return Resolution(tuple(matches), stage=stage)
