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

Each item has its own request_id, logged when it is answered (8.39.1). When
both the held request and the answer carry a Home Assistant user, they must
be the same person, or nothing runs.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

_LOGGER = logging.getLogger(__name__)

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
    request_id: str = ""


_ITEMS: dict[str, Pending] = {}


def put(device_id: str, kind: str, ref: str, user_id: Optional[str] = None,
        *, now: Optional[float] = None) -> Optional[Pending]:
    """Record what a yes on this device would run. Replaces any older item
    for the device. Does nothing without a device or a reference."""
    if not device_id or not ref or kind not in (KIND_PHRASE, KIND_SCENE_SCRIPT):
        return None
    t = time.monotonic() if now is None else now
    item = Pending(str(device_id), kind, str(ref), str(user_id or ""), t + TTL_SECONDS,
                   uuid.uuid4().hex)
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


# ── the answer, on the next turn from the same device ───────────────────────
# The whole utterance must be one of these, after voice_phrases.normalise
# (case, punctuation, "Nova" and a trailing "please" do not matter, so "Yes."
# counts). Deliberately not voice_confirm._YES: that list has "unlock it" and
# "open it", which must never read as a yes here.
YES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "yes please", "please do",
    "do it", "go ahead", "yes go ahead", "go for it", "confirm", "confirmed",
    "affirmative", "yes do it", "yes it is",
})
NO = frozenset({
    "no", "nope", "no thanks", "no thank you", "cancel", "do not", "stop",
    "leave it", "never mind", "nevermind", "negative", "not now", "no do not",
})
NO_REPLY = "OK, I won't."


def _addr(honorific: str) -> str:
    return f", {honorific}" if honorific else ""


def _result(text: str, success: bool):
    from .local_engine import LocalResult
    return LocalResult(text=text, success=success, handled=True)


async def answer(hass, device_id: str, text: str, *, honorific: str = "",
                 user_id: Optional[str] = None):
    """Handle this device's held request, if it has one. A clear yes runs it
    once, approved for exactly its own targets; a clear no drops it; anything
    else drops it and returns None so the turn carries on as normal. Returns
    a local_engine.LocalResult for the reply, or None. Only this device's
    item is read or cleared. Never raises: any error runs nothing.

    The yes can still be lost before it gets here, which is safe: nothing
    runs. The 4 second duplicate check (keyed on the words) can give it to
    another satellite that heard it first, and the opt in presence gate can
    drop it.

    When the held request and this turn both name a user and they differ,
    the item is dropped and nothing runs."""
    try:
        item = get(device_id)
        if item is None:
            return None
        if item.user_id and user_id and str(user_id) != item.user_id:
            clear(device_id)
            _LOGGER.info("Held request %s dropped: answered by a different user",
                         item.request_id)
            return None
        from .voice_phrases import normalise
        said = normalise(text)
        if said in NO:
            clear(device_id)
            return _result(NO_REPLY, True)
        if said not in YES:
            clear(device_id)
            return None
        clear(device_id)
        _LOGGER.info("Held request %s confirmed on %s", item.request_id, item.device_id)
        if item.kind == KIND_PHRASE:
            return await _replay_phrase(hass, item, honorific)
        return await _replay_scene_script(hass, item, honorific)
    except Exception:  # pylint: disable=broad-except
        clear(device_id)
        return _result(f"I couldn't do that{_addr(honorific)}.", False)


async def _replay_phrase(hass, item: Pending, honorific: str):
    import copy
    from . import nova_config, policy, voice_phrases, voice_phrase_runner
    cfg = copy.deepcopy(nova_config.get(voice_phrases.CONFIG_KEY))
    entry = voice_phrases.find_entry(cfg, item.ref)
    service = str(((entry or {}).get("action") or {}).get("service") or "")
    if (not isinstance(cfg, dict) or cfg.get("enabled", True) is False or entry is None
            or entry.get("enabled", True) is False
            or service.startswith(voice_phrases.SHELL_DOMAIN + ".")):
        return _result(f"That voice phrase isn't available any more{_addr(honorific)}.", False)
    domain, _, svc = service.partition(".")
    # Every target the phrase runs, built the way run_entry builds them, so a
    # phrase with several targets is approved for all of them.
    targets = [(domain, svc, k) for k in voice_phrase_runner._keys(domain, svc, entry)]
    with policy.approved_confirmation(item.device_id, targets):
        outcome = await voice_phrase_runner.run_entry(
            hass, entry, device_id=item.device_id, user_id=item.user_id,
            honorific=honorific, may_ask=True)
    if outcome.result is not None:
        return outcome.result
    return _result(f"I couldn't run that{_addr(honorific)}.", False)


async def _replay_scene_script(hass, item: Pending, honorific: str):
    import json
    from . import policy
    from .agent_runtime.capabilities.control import _exec_run_scene_script
    domain = item.ref.split(".", 1)[0]
    svc = "turn_on" if domain in ("scene", "script") else "trigger"
    with policy.approved_confirmation(item.device_id, [(domain, svc, item.ref)]):
        res = json.loads(await _exec_run_scene_script(
            hass, {"entity_id": item.ref}, device_id=item.device_id,
            user_id=item.user_id or None))
    if res.get("success"):
        return _result(f"Done{_addr(honorific)}.", True)
    return _result(f"I couldn't do that{_addr(honorific)}.", False)
