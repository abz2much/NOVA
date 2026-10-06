"""Turn a model reply into plain text that is fit to be spoken.

Replies are read out by a speaker and shown in Spoken History, neither of
which renders markdown, so ``**bold**`` and ``- `` bullets would otherwise
appear (or be spoken) literally. A leaf module: stdlib only, never raises.
"""
from __future__ import annotations

import re

_FENCE = re.compile(r"```[^\n]*\n?(.*?)```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_TICKS = re.compile(r"`([^`]*)`")
_INLINE_BULLET = re.compile(r"(?<=[:?.!])\s+[-*•]\s+(?=\S)")
_SPACES = re.compile(r"[ \t]+")


def speech_text(text):
    """``text`` with markdown removed and lists turned into flowing sentences."""
    if not isinstance(text, str) or not text:
        return text
    try:
        out = _FENCE.sub(r"\1", text)
        out = _LINK.sub(r"\1", out)
        out = _HEADING.sub("", out)
        out = _BULLET.sub("", out)
        # Bullets the model ran together on one line: "...? - **A** — b - **B**"
        out = _INLINE_BULLET.sub(" ", out)
        out = _BOLD.sub(r"\2", out)
        out = _ITALIC.sub(r"\1", out)
        out = _TICKS.sub(r"\1", out)
        out = out.replace("**", "").replace("__", "")
        out = re.sub(r"\n{2,}", ". ", out)
        out = out.replace("\n", ", ")
        out = _SPACES.sub(" ", out)
        out = re.sub(r"([.?!:]),\s", r"\1 ", out)
        out = re.sub(r"\.\s*\.", ".", out)
        return out.strip()
    except Exception:
        return text
