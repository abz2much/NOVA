"""Where Nova keeps its files: the single source of truth.

Every Nova store (SQLite databases, JSON state, logs, documents) lives under
Home Assistant's own config directory. This module owns that directory, so
no other module hard codes ``/config``. ``configure(hass)`` is called first in
async_setup_entry, before anything reads or writes a store; until then the
root defaults to ``/config``, which is where HA OS and HA Container keep it.

Modules resolve their paths when they use them (never at import time), so a
call after configure() always sees this instance's real directory. A module
that lets callers or tests point it elsewhere keeps an override attribute
that defaults to ``None`` and falls back to the helpers here.
"""
from __future__ import annotations

import os

_DEFAULT_ROOT = "/config"
_root = _DEFAULT_ROOT

# Files shared by several modules, named once here.
PATTERNS_DB = "patterns.db"            # under nova/
CONVERSATIONS_DB = "conversations.db"  # under nova/
NOVA_DB = "nova.db"                    # at the config root (memory, documents, embeddings)
MEMORY_DIR = "nova_memory"             # at the config root (ChromaDB)
LEARNED_FILE = ".nova_learned.json"    # at the config root (learned aliases)


def configure(hass) -> None:
    """Point Nova at this Home Assistant instance's config directory."""
    global _root
    _root = str(hass.config.path() or _DEFAULT_ROOT)


def config_dir() -> str:
    """Home Assistant's config directory."""
    return _root


def config_path(*parts: str) -> str:
    """A path under Home Assistant's config directory."""
    return os.path.join(_root, *parts)


def nova_path(*parts: str) -> str:
    """A path under Nova's own folder, <config>/nova."""
    return os.path.join(_root, "nova", *parts)


def patterns_db() -> str:
    return nova_path(PATTERNS_DB)


def conversations_db() -> str:
    return nova_path(CONVERSATIONS_DB)


def nova_db() -> str:
    return config_path(NOVA_DB)


def memory_dir() -> str:
    return config_path(MEMORY_DIR)


def learned_file() -> str:
    return config_path(LEARNED_FILE)
