"""Removal of leaked hidden reasoning from visible model text.

Reasoning models can place their private scratchpad in the ordinary content
field (a ``<think>…</think>`` envelope). Adapters never read dedicated
reasoning fields, and every visible text passes through visible_text()
before it becomes part of a ChatResponse.
"""
from __future__ import annotations

from typing import Any


def visible_text(content: Any) -> str:
    """Remove a leaked reasoning envelope from response text.

    Some reasoning models place their scratchpad in the content. Qwen can
    also omit the opening ``<think>`` marker while retaining a line-delimited
    closing marker. Strip only those two well-defined envelope shapes; an
    inline literal ``</think>`` remains ordinary user-visible text. A reply
    that opens a ``<think>`` envelope and never closes it (cut off by the
    token limit) is all reasoning, so none of it is visible.
    """
    text = str(content or "").strip()
    lower = text.lower()
    marker = "</think>"
    marker_at = lower.find(marker)
    if marker_at < 0:
        return "" if lower.startswith("<think>") else text

    prefix = text[:marker_at]
    suffix = text[marker_at + len(marker):]
    has_opening_marker = prefix.lstrip().lower().startswith("<think>")
    has_orphan_line_marker = (
        (marker_at == 0 or prefix.endswith(("\n", "\r")))
        and suffix.startswith(("\n", "\r"))
    )
    if has_opening_marker or has_orphan_line_marker:
        return suffix.strip()
    return text
