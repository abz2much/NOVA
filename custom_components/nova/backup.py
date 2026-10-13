"""Archive and restore Nova's on-disk state.

``create_backup`` packs Nova's state files from the Home Assistant config
directory into a timestamped ``.tar.gz`` under ``nova_backups``;
``restore_backup`` unpacks one (the newest by default) back into the config
directory. Both are blocking and meant to run in an executor."""

from __future__ import annotations

import glob
import logging
import os
import tarfile
import tempfile
import threading
import time

_LOGGER = logging.getLogger(__name__)

# Top-level names (relative to the config dir) that make up Nova's state.
_STATE_PATHS = [
    "nova",
    "nova.db",
    "nova_memory",
    "nova_persona.txt",
    "nova_routines.yaml",
    ".nova_learned.json",
    ".nova_ignore_rules.json",
]
_BACKUP_SUBDIR = "nova_backups"

# Held while an archive takes its final name, so two backups in the same
# second cannot pick the same one.
_NAME_LOCK = threading.Lock()


def _inside_state(config_dir: str, link: str) -> bool:
    """True when ``link`` resolves to a place inside one of the state paths."""
    target = os.path.realpath(link)
    root = os.path.realpath(config_dir)
    for name in _STATE_PATHS:
        allowed = os.path.join(root, name)
        if os.path.commonpath([allowed, target]) == allowed:
            return True
    return False


def create_backup(config_dir: str) -> str:
    """Write a gzip tarball of the present state paths and return its path."""
    backup_dir = os.path.join(config_dir, _BACKUP_SUBDIR)
    os.makedirs(backup_dir, exist_ok=True)

    def skip_outside_links(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if info.issym() and not _inside_state(config_dir, os.path.join(config_dir, info.name)):
            _LOGGER.warning("Backup skipped %s: it is a link to outside Nova's data", info.name)
            return None
        return info

    stamp = time.strftime("%Y%m%d-%H%M%S")

    # Written under a temporary name (owner-only from creation, and not
    # matched by the restore glob), then moved into place only on success.
    fd, tmp = tempfile.mkstemp(prefix=".nova-state-", suffix=".tmp", dir=backup_dir)
    try:
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            for name in _STATE_PATHS:
                source = os.path.join(config_dir, name)
                if os.path.exists(source):
                    tar.add(source, arcname=name, filter=skip_outside_links)

        with _NAME_LOCK:
            path = os.path.join(backup_dir, f"nova-state-{stamp}.tar.gz")
            count = 1
            while os.path.lexists(path):
                path = os.path.join(backup_dir, f"nova-state-{stamp}-{count}.tar.gz")
                count += 1
            os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def restore_backup(config_dir: str, archive: str = "") -> str:
    """Extract an archive (default: newest by modification time) into ``config_dir``."""
    backup_dir = os.path.join(config_dir, _BACKUP_SUBDIR)

    if not archive:
        candidates = glob.glob(os.path.join(backup_dir, "nova-state-*.tar.gz"))
        if not candidates:
            raise FileNotFoundError(f"no Nova backups found in {backup_dir}")
        path = max(candidates, key=lambda p: (os.stat(p).st_mtime_ns, p))
    else:
        path = archive if os.path.isabs(archive) else os.path.join(backup_dir, archive)
        if not os.path.exists(path):
            raise FileNotFoundError(path)

    with tarfile.open(path, "r:gz") as tar:
        tar.extractall(config_dir, filter="data")
    return path
