"""Small JSON state files: reads that tell missing from corrupt, and
atomic replacement.

Owners keep their fail-safe behaviour (a missing or corrupt file still
loads as their empty default); what changes is that a corrupt file is now
reported as corrupt in the log instead of looking like a fresh install,
and that no writer can leave a half-written file behind.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

StrPath = Union[str, "os.PathLike[str]"]

MISSING = "missing"
OK = "ok"
CORRUPT = "corrupt"


@dataclass(frozen=True)
class JsonRead:
    status: str            # MISSING, OK or CORRUPT
    value: Any = None
    error: str = ""


def read_json(path: StrPath, *, encoding: Optional[str] = None) -> JsonRead:
    # Check first, as every owner did: an absent file is never opened.
    if not os.path.exists(path):
        return JsonRead(MISSING)
    try:
        with open(path, encoding=encoding) as f:
            return JsonRead(OK, json.load(f))
    except FileNotFoundError:
        return JsonRead(MISSING)
    except Exception as exc:
        return JsonRead(CORRUPT, error=f"{type(exc).__name__}: {exc}")


def write_json_atomic(path: StrPath, data: Any, *, indent: Optional[int] = None,
                      encoding: Optional[str] = None) -> None:
    """Write `data` to a temporary file beside `path`, then rename it into
    place, so readers only ever see the old or the new complete file.
    Raises on failure; the temporary file is removed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix=f".{target.name}.", suffix=".tmp")
    try:
        try:
            mode = target.stat().st_mode & 0o777   # keep an existing file's mode
        except OSError:
            mode = 0o644                          # what a plain open() gave before
        os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding=encoding) as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
