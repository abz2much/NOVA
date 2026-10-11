"""A request waiting for a spoken yes, one per voice satellite (8.33.0).

When a request made on a voice satellite needs confirming, Nova never asks
separately (that would cancel the user's own running request). Its reply
asks instead, and the tool that was held records here what to run if the
user says yes: a voice phrase by its phrase_id, or a scene, script or
automation by its entity_id. The conversation reads it on the next turn
from the same device only.

In memory only: nothing survives a restart. An item lasts 60 seconds, and a
newer one for the same device replaces the older one, so when the AI calls
several tools in one turn the last one wins.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

TTL_SECONDS = 60.0
KIND_PHRASE = "voice_phrase"
KIND_SCENE_SCRIPT = "scene_script"
# The question every held voice request ends its reply with, so the reply
# reads as a question and the mic reopens for the answer.
ASK = "Shall I go ahead?"


@dataclass(frozen=True)
class Pending:
    device_id: str
    kind: str          # KIND_PHRASE or KIND_SCENE_SCRIPT
    ref: str           # the phrase_id, or the scene/script/automation entity_id
    user_id: str
    expires: float     # time.monotonic() deadline


_ITEMS: dict[str, Pending] = {}


def put(device_id: str, kind: str, ref: str, user_id: Optional[str] = None,
        *, now: Optional[float] = None) -> Optional[Pending]:
    """Record what a yes on this device would run. Replaces any older item
    for the device. Does nothing without a device or a reference."""
    if not device_id or not ref or kind not in (KIND_PHRASE, KIND_SCENE_SCRIPT):
        return None
    t = time.monotonic() if now is None else now
    item = Pending(str(device_id), kind, str(ref), str(user_id or ""), t + TTL_SECONDS)
    _ITEMS[item.device_id] = item
    return item


def get(device_id: str, *, now: Optional[float] = None) -> Optional[Pending]:
    """This device's item, or None. An expired item is dropped."""
    if not device_id:
        return None
    item = _ITEMS.get(str(device_id))
    if item is None:
        return None
    if (time.monotonic() if now is None else now) >= item.expires:
        _ITEMS.pop(item.device_id, None)
        return None
    return item


def pop(device_id: str, *, now: Optional[float] = None) -> Optional[Pending]:
    """Take this device's item (None when there is none or it expired)."""
    item = get(device_id, now=now)
    _ITEMS.pop(str(device_id or ""), None)
    return item


def clear(device_id: str) -> None:
    _ITEMS.pop(str(device_id or ""), None)


def clear_all() -> None:
    _ITEMS.clear()
