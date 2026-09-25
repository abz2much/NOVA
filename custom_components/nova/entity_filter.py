"""User-configurable exclusion of entities from Nova's awareness.

An entity can be excluded three ways, matching how people think about their
setups:

  * ``excluded_entities`` — specific entity_ids, one by one.
  * ``excluded_domains``  — whole domains (e.g. ``light``, ``switch``).
  * ``excluded_labels``   — every entity carrying a Home Assistant label
                            (the "as a group" case).

Excluded entities are dropped everywhere Nova enumerates entities on its own —
presence detection, room routing, the observer, and pattern/state learning — so
noise sources (a virtual occupancy sensor that other integrations expose, unused
light/switch entities) stop polluting those systems. Exclusion removes an entity
from Nova's *awareness*; Home Assistant still has the entity and Nova can
still act on it if you ask for it by name.

Config is read from the in-memory ``runtime_config`` (seeded from config.json at
setup, kept current by the panel), never via ``nova_config.get()``, because
``is_excluded()`` runs on the hot state-change path and a lazy config load there
both costs time and mutates shared module state. runtime_config belongs to the
Nova entry's NovaRuntime and is read on the event loop; code running in an
executor passes an ``exclusion_snapshot()`` taken on the loop instead.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

try:  # label registry is present on modern HA; degrade gracefully if not
    from homeassistant.helpers import label_registry as _lr
except Exception:  # pragma: no cover - very old HA
    _lr = None

_LOGGER = logging.getLogger(__name__)


def _as_list(value) -> list:
    """Coerce a config value (list, JSON string, or scalar) into a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else [s]
        except Exception:
            return [s]
    return []


def _exclusion_config(hass: HomeAssistant):
    """Return (entities:set, domains:set, labels:set) from runtime_config.

    Reads the Nova entry's live runtime_config, so call it on the event
    loop. The sets are new on every call."""
    from .runtime import domain_runtime_config
    rc = domain_runtime_config(hass) or {}
    ents: set[str] = set()
    doms: set[str] = set()
    labs: set[str] = set()
    try:
        for e in _as_list(rc.get("excluded_entities")):
            if e:
                ents.add(str(e))
        for d in _as_list(rc.get("excluded_domains")):
            if d:
                doms.add(str(d).strip().lower())
        for lab in _as_list(rc.get("excluded_labels")):
            if lab:
                labs.add(str(lab))
    except Exception:
        pass
    return ents, doms, labs


def exclusion_snapshot(hass: HomeAssistant) -> tuple[set, set, set]:
    """The current exclusions as new sets, for handing to an executor job.

    Take it on the event loop right before the job and pass it to
    is_excluded(..., exclusions=...), so the worker thread never reads the
    live runtime_config. Never reuse it for a later operation."""
    return _exclusion_config(hass)


def is_excluded(hass: HomeAssistant, entity_id: Optional[str],
                exclusions: Optional[tuple] = None) -> bool:
    """True if the user has excluded this entity (by id, domain, or label).

    Cheap and safe to call on the hot state-change path: a tiny dict read plus,
    only when labels are configured, an in-memory registry lookup. From an
    executor thread, pass `exclusions` (an exclusion_snapshot() taken on the
    event loop) so the live runtime_config is never read there.
    """
    if not entity_id:
        return False
    ents, doms, labs = (exclusions if exclusions is not None
                        else _exclusion_config(hass))
    if not ents and not doms and not labs:
        return False
    if entity_id in ents:
        return True
    domain = entity_id.split(".", 1)[0].lower()
    if domain in doms:
        return True
    if labs:
        try:
            ent = er.async_get(hass).async_get(entity_id)
            ent_labels = getattr(ent, "labels", None) if ent else None
            if ent_labels:
                # Match by label_id directly …
                if set(ent_labels) & labs:
                    return True
                # … and by human-readable label name, so a config that stored
                # names (or ids) both work.
                if _lr is not None:
                    reg = _lr.async_get(hass)
                    for lid in ent_labels:
                        lbl = reg.async_get_label(lid)
                        if lbl is not None and getattr(lbl, "name", None) in labs:
                            return True
        except Exception:
            pass
    return False


def excluded_entity_ids(hass: HomeAssistant) -> set:
    """The concrete set of currently-excluded entity_ids, expanding domains and
    labels across the live registry. For UI / bulk use — NOT the hot path, where
    ``is_excluded`` should be called per entity instead.
    """
    ents, doms, labs = _exclusion_config(hass)
    out = set(ents)
    if not doms and not labs:
        return out
    try:
        for st in hass.states.async_all():
            eid = st.entity_id
            if eid not in out and is_excluded(hass, eid):
                out.add(eid)
    except Exception:
        pass
    return out
