"""Validation of a provider's reasoning reply.

A reply becomes a Decision only when it is a JSON object whose "speak" is a
boolean and, when speaking, carries a non-empty message. Anything else is a
ProviderFailure: the caller treats it exactly like an unavailable provider
(the Local Mind decides) and never learns from it. A failure names what was
wrong and the reply's length, never the reply's words. Pure.
"""
from __future__ import annotations

import json
import re
from typing import Union

from .models import (
    ACTION_SILENT,
    ACTION_SPEAK,
    Decision,
    FAIL_EMPTY_MESSAGE,
    FAIL_INCOMPLETE,
    FAIL_INVALID,
    FAIL_UNREADABLE,
    ORIGIN_PROVIDER,
    ProviderFailure,
    R_PROVIDER_SILENT,
    R_PROVIDER_SPEAK,
    URGENCIES,
)


def extract_json_text(raw: str) -> str:
    """Strip code fences and surrounding prose around the first {...} span."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start : end + 1]
    return raw


def parse_reply(text, *, classifier_urgency: str) -> Union[Decision, ProviderFailure]:
    if not isinstance(text, str):
        return ProviderFailure(FAIL_UNREADABLE, "reply is not text", 0)
    length = len(text)
    try:
        obj = json.loads(extract_json_text(text))
    except (json.JSONDecodeError, ValueError):
        return ProviderFailure(FAIL_UNREADABLE, "reply is not JSON", length)
    if not isinstance(obj, dict):
        return ProviderFailure(FAIL_UNREADABLE, "reply is not a JSON object", length)
    if "speak" not in obj:
        return ProviderFailure(FAIL_INCOMPLETE, "reply has no speak field", length)
    if not isinstance(obj["speak"], bool):
        return ProviderFailure(FAIL_INVALID, "speak is not a boolean", length)
    if not obj["speak"]:
        reason = obj.get("reason", "reasoning declined")
        reason = "reasoning declined" if reason is None else str(reason)
        return Decision(ACTION_SILENT, R_PROVIDER_SILENT, reason=reason, evaluator="provider",
                        origin=ORIGIN_PROVIDER, provider_used=True, cacheable=True,
                        validated=True)
    message = obj.get("message")
    if not isinstance(message, str) or not message.strip():
        return ProviderFailure(FAIL_EMPTY_MESSAGE, "speak without a message", length)
    urgency = obj.get("urgency", classifier_urgency)
    if urgency not in URGENCIES:
        urgency = classifier_urgency
    return Decision(ACTION_SPEAK, R_PROVIDER_SPEAK, urgency=urgency, message=message.strip(),
                    evaluator="provider", origin=ORIGIN_PROVIDER, provider_used=True,
                    cacheable=urgency in URGENCIES, validated=True)
