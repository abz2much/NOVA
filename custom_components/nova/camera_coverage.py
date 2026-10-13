"""Which rooms a camera can see well enough to confirm a person.

The floor-plan panel traces sightlines and hands over how much of each room
is in view. The reasoning model judges which rooms count as covered and
describes the view in one sentence; geometry alone is the fallback. The
result is internal structured data, not speech.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

_CONFIRMABLE = 0.7

_SYSTEM_PROMPT = (
    "You judge what a home security camera can see, to help confirm whether an "
    "intruder is present. You are given a geometric estimate of how much of each "
    "room is in the camera's view. Sightlines have already been traced through "
    "open doorways and cased openings and are blocked by solid walls. Decide "
    "which rooms are covered well enough to confirm a person is there: about 70% "
    "or more of a room is confirmable, a small sliver is not. Write one short, "
    "plain sentence describing the view. Reply with only a JSON object of the form "
    '{"covered": ["Room", ...], "reason": "..."} with no other text and no code '
    "fences. Use only the room names given; do not invent rooms."
)

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\s*```$")
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _fraction(value: Any) -> float:
    return float(value or 0)


def _by_fraction(candidates: dict) -> list:
    return sorted(candidates.items(), key=lambda kv: _fraction(kv[1]), reverse=True)


def plain_reason(candidates: dict) -> str:
    parts = []
    for room, frac in _by_fraction(candidates):
        pct = round(_fraction(frac) * 100)
        if pct >= 70:
            parts.append(f"most of {room}")
        elif pct >= 30:
            parts.append(f"part of {room}")
        else:
            parts.append(f"a corner of {room}")
    if not parts:
        return "Nothing in view."
    return "Sees " + ", ".join(parts) + "."


def _geometry_result(candidates: dict) -> dict:
    return {
        "covered": [room for room, frac in candidates.items()
                    if _fraction(frac) >= _CONFIRMABLE],
        "reason": plain_reason(candidates),
        "source": "geometry",
        "ts": int(time.time()),
    }


def _user_prompt(cam_ctx: dict, candidates: dict) -> str:
    lines = [f"The camera is in {cam_ctx.get('room') or 'an unknown room'}."]
    indoor = cam_ctx.get("indoor", True)
    range_ft = cam_ctx.get("range_ft")
    if not indoor and range_ft:
        lines.append(f"It is outdoors and sees up to about {range_ft} ft.")
    lines.append(f"Field of view: {cam_ctx.get('fov') or 90} degrees.")
    lines.append("Estimated share of each room in view:")
    for room, frac in _by_fraction(candidates):
        lines.append(f"- {room}: {round(_fraction(frac) * 100)}%")
    openings = cam_ctx.get("openings") or []
    if openings:
        lines.append("Open connections: " + ", ".join(str(o) for o in openings))
    lines.append("Which rooms can this camera confirm a person in? "
                 "Describe the view in one sentence.")
    return "\n".join(lines)


def _parse(text: str, candidates: dict) -> Optional[dict]:
    if not text:
        return None
    body = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text.strip()))
    span = _OBJECT.search(body)
    if span:
        body = span.group(0)
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    names = {str(room).lower(): room for room in candidates}
    raw = obj.get("covered")
    covered = []
    for entry in raw if isinstance(raw, list) else []:
        room = names.get(str(entry).lower())
        if room is not None:
            covered.append(room)

    reason = str(obj.get("reason") or "").strip()
    if not reason:
        return None
    return {"covered": covered, "reason": reason, "source": "llm",
            "ts": int(time.time())}


async def infer_coverage(hass: Any, config: dict, cam_ctx: dict) -> dict:
    """Judge a camera's coverage, falling back to geometry on any failure."""
    candidates = (cam_ctx or {}).get("candidates") or {}
    if not candidates:
        return {"covered": [], "reason": "Nothing in view.", "source": "geometry",
                "ts": int(time.time())}

    fallback = _geometry_result(candidates)
    try:
        from . import llm_provider
        from .providers.activity import execute_chat
        from .providers.manager import provider_scope
        from .providers.routing import tier_spec

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(cam_ctx, candidates)},
        ]
        async with provider_scope(hass) as providers:
            async with providers.lease(
                tier_spec(config, "reasoning"),
                factory=lambda: llm_provider.create_tier_provider(config, "reasoning"),
            ) as client:
                result = await execute_chat(
                    hass, client, messages, role="coverage", data_category="text",
                    max_tokens=400, temperature=0.3)
        parsed = _parse(result.text or "", candidates)
    except Exception as exc:
        _LOGGER.debug("Camera coverage model call failed: %s", exc)
        return fallback
    return parsed if parsed is not None else fallback


def _match_camera(by_floor: Any, area_name: Any) -> Optional[str]:
    target = str(area_name or "").strip().lower()
    if not target:
        return None
    for cameras in (by_floor or {}).values():
        for camera in cameras or []:
            entity = camera.get("entity")
            if not entity:
                continue
            covered = (camera.get("coverage") or {}).get("covered") or []
            for raw in covered:
                room = str(raw).strip().lower()
                if room and (room == target or target in room or room in target):
                    return entity
    return None


def camera_for_area(hass: Any, area_name: Any) -> Optional[str]:
    """The saved camera whose confirmed coverage includes ``area_name``."""
    if not area_name:
        return None
    try:
        from . import nova_config
        raw = nova_config.get("floor_plan_cameras", {})
        plan = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return _match_camera(plan, area_name)
    except Exception:
        return None
