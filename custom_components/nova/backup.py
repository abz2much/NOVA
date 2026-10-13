"""Archive and restore Nova's on-disk state.

``create_backup`` packs Nova's state files from the Home Assistant config
directory into a timestamped ``.tar.gz`` under ``nova_backups``;
``restore_backup`` unpacks one (the newest by default) back into the config
directory. Both are blocking and meant to run in an executor."""

from __future__ import annotations

import glob
import os
import tarfile
import time

# Top-level names (relative to the config dir) that make up Nova's state.
_STATE_PATHS = [
    "nova",
    "nova.db",
    "nova_memory",
    "nova_persona.txt",
    "nova_routines.yaml",
]
_BACKUP_SUBDIR = "nova_backups"


def create_backup(config_dir: str) -> str:
    """Write a gzip tarball of the present state paths and return its path."""
    backup_dir = os.path.join(config_dir, _BACKUP_SUBDIR)
    os.makedirs(backup_dir, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, f"nova-state-{stamp}.tar.gz")

    # Owner-only from creation; fchmod also tightens a pre-existing file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
        for name in _STATE_PATHS:
            source = os.path.join(config_dir, name)
            if os.path.exists(source):
                tar.add(source, arcname=name)
    return path


def restore_backup(config_dir: str, archive: str = "") -> str:
    """Extract an archive (default: newest by name) into ``config_dir``."""
    backup_dir = os.path.join(config_dir, _BACKUP_SUBDIR)

    if not archive:
        candidates = glob.glob(os.path.join(backup_dir, "nova-state-*.tar.gz"))
        if not candidates:
            raise FileNotFoundError(f"no Nova backups found in {backup_dir}")
        path = max(candidates)
    else:
        path = archive if os.path.isabs(archive) else os.path.join(backup_dir, archive)
        if not os.path.exists(path):
            raise FileNotFoundError(path)

    with tarfile.open(path, "r:gz") as tar:
        try:
            tar.extractall(config_dir, filter="data")
        except TypeError:
            # Python without extraction filters.
            tar.extractall(config_dir)
    return path
