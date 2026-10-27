"""Entities the user has taken out of Nova's awareness.

Three ways to exclude, matching how people think about their homes: single
entities (``excluded_entities``), whole domains (``excluded_domains``, e.g.
``light``) and everything carrying a Home Assistant label
(``excluded_labels``, the "as a group" case).

Wherever Nova lists entities by itself (presence, room routing, the
observer, pattern and state learning) excluded ones are left out, so noise
such as a virtual occupancy sensor or unused switches stops leaking in. Home
Assistant keeps the entity, and Nova still acts on it when asked by name.

The settings come from the in-memory ``runtime_config`` (seeded from
config.json at setup and kept current by the panel), never from
``nova_config.get()``: ``is_excluded()`` sits on the busy state change path,
where a lazy load would cost time and change shared state. That config
belongs to the entry's NovaRuntime and is read on the event loop; executor
code passes an ``exclusion_snapshot()`` taken on the loop instead.
"""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

try:  # every current Home Assistant has the label registry
    from homeassistant.helpers import label_registry as _lr
except Exception:  # pragma: no cover - very old Home Assistant
    _lr = None

_LOGGER = logging.getLogger(__name__)


def _as_list(value) -> list:
    """A setting as a list: a list as is, a JSON list string parsed, any
    other non empty string as one item, anything else empty."""
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return [text]
    return parsed if isinstance(parsed, list) else [text]


def _exclusion_config(hass: HomeAssistant):
    """(entities, domains, labels) as fresh sets from the live runtime
    config; call it on the event loop."""
    from .runtime import domain_runtime_config
    live = domain_runtime_config(hass) or {}
    entities: set[str] = set()
    domains: set[str] = set()
    labels: set[str] = set()
    try:
        entities.update(str(e) for e in _as_list(live.get("excluded_entities")) if e)
        domains.update(str(d).strip().lower()
                       for d in _as_list(live.get("excluded_domains")) if d)
        labels.update(str(lab) for lab in _as_list(live.get("excluded_labels")) if lab)
    except Exception:
        pass
    return entities, domains, labels


def exclusion_snapshot(hass: HomeAssistant) -> tuple[set, set, set]:
    """The exclusions right now, for one executor job. Take it on the loop
    just before the job and pass it as ``exclusions=``, so the worker never
    reads the live config; never keep it for a later job."""
    return _exclusion_config(hass)


def _carried_labels(hass, entity_id: str) -> set:
    """The label ids on the entity and on its device (8.82.0): most homes
    label devices, and an entity inherits its device's labels."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return set()
    carried = set(getattr(entry, "labels", None) or ())
    device_id = getattr(entry, "device_id", None)
    if device_id:
        try:
            device = dr.async_get(hass).async_get(device_id)
            carried |= set(getattr(device, "labels", None) or ())
        except Exception:
            pass
    return carried


def _label_match(hass, entity_id: str, labels: set) -> bool:
    """The entity, or its device, carries one of the labels, given by id or
    by name."""
    carried = _carried_labels(hass, entity_id)
    if not carried:
        return False
    if set(carried) & labels:
        return True
    if _lr is None:
        return False
    registry = _lr.async_get(hass)
    for label_id in carried:
        label = registry.async_get_label(label_id)
        if label is not None and getattr(label, "name", None) in labels:
            return True
    return False


# ── what the home's own labels say a sensor is (8.82.0) ─────────────────────
# Three labels beat any guess from a name: Appliance (never a way in), Entry
# Point (a way in) and Group (repeats other sensors, so never counted).
ROLE_APPLIANCE = "appliance"
ROLE_ENTRY_POINT = "entry_point"
ROLE_GROUP = "group"


def _role_key(text) -> str:
    """"Entry Point", "entry-point" and "entry_point" are one key."""
    return "_".join(str(text or "").strip().lower().replace("-", " ").split())


def label_roles(hass, entity_id: str | None) -> set:
    """Every label on the entity and its device, by id and by name, as
    keys to compare with the ROLE_ names. Empty on any error."""
    if not entity_id:
        return set()
    try:
        carried = _carried_labels(hass, entity_id)
        keys = {_role_key(lid) for lid in carried}
        if _lr is not None and carried:
            registry = _lr.async_get(hass)
            for label_id in carried:
                label = registry.async_get_label(label_id)
                if label is not None:
                    keys.add(_role_key(getattr(label, "name", "")))
        return keys
    except Exception:
        return set()


def is_group_sensor(hass, state) -> bool:
    """A sensor that repeats others: labelled Group, or a Home Assistant
    group (it lists its members in an entity_id attribute)."""
    members = state.attributes.get("entity_id")
    if isinstance(members, (list, tuple)) and members:
        return True
    return ROLE_GROUP in label_roles(hass, state.entity_id)


def is_excluded(hass: HomeAssistant, entity_id: str | None,
                exclusions: tuple | None = None) -> bool:
    """Whether the user excluded this entity, by id, domain or label.

    Cheap enough for the state change path: a small dict read, plus an
    in-memory registry lookup only when labels are set. In an executor,
    pass ``exclusions`` (a snapshot taken on the loop).
    """
    if not entity_id:
        return False
    entities, domains, labels = (exclusions if exclusions is not None
                                 else _exclusion_config(hass))
    if entity_id in entities or entity_id.split(".", 1)[0].lower() in domains:
        return True
    if not labels:
        return False
    try:
        return _label_match(hass, entity_id, labels)
    except Exception:
        return False


# ── object sensors are not people (8.14.0) ──────────────────────────────────
# Camera integrations such as Frigate expose one binary_sensor per detected
# object class, for example binary_sensor.garage_car_occupancy or
# binary_sensor.driveway_bicycle_occupancy, with device_class occupancy. They say an
# object is in view, not that a person is there, so they must never decide
# that someone is home or in a room. Their own events are unaffected.
_OBJECT_LABELS = frozenset({
    "car", "truck", "bus", "van", "vehicle", "motorcycle", "motorbike",
    "bicycle", "bike", "boat",
    "dog", "cat", "bird", "horse", "animal", "pet", "deer", "fox",
    "package", "parcel",
})
_DETECTION_WORDS = frozenset({
    "occupancy", "occupied", "motion", "presence", "detected", "detection", "active",
})
_PERSON_WORDS = frozenset({"person", "people", "human"})


def _words(text) -> list:
    out, cur = [], []
    for ch in str(text).lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


_APPLIANCE_WORDS = frozenset({
    "fridge", "refrigerator", "freezer", "oven", "dishwasher", "washer",
    "washing", "dryer", "microwave", "cabinet",
})


def is_appliance_opening(entity_id, friendly_name=None) -> bool:
    """True when a door or window sensor belongs to an appliance or a cabinet
    (a fridge door, an oven door), which is never a way into the house. Matched
    on whole words of the entity id or the name. Pure, never raises."""
    try:
        for text in (str(entity_id or "").split(".", 1)[-1], friendly_name or ""):
            if _APPLIANCE_WORDS.intersection(_words(text)):
                return True
    except Exception:
        return False
    return False


def is_object_sensor(entity_id, friendly_name=None) -> bool:
    """True when a sensor reports a detected object (a car, an animal or a
    package) rather than a person: an object word that ends the name or comes
    right before a detection word ("garage car occupancy", "doorbell pet
    detected"). A name with a person word is never one. Pure, never raises."""
    try:
        for text in (str(entity_id or "").split(".", 1)[-1], friendly_name or ""):
            words = _words(text)
            if not words or _PERSON_WORDS.intersection(words):
                continue
            for i, word in enumerate(words):
                if word in _OBJECT_LABELS and (
                        i == len(words) - 1 or words[i + 1] in _DETECTION_WORDS):
                    return True
    except Exception:
        return False
    return False
