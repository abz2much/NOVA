"""Archive and restore Nova's on-disk state.

``create_backup`` packs Nova's state files from the Home Assistant config
directory into a timestamped ``.tar.gz`` under ``nova_backups``;
``restore_backup`` unpacks one (the newest by default) back into the config
directory. Both are blocking and meant to run in an executor."""

from __future__ import annotations

import glob
import logging
import os
import posixpath
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


def _archive_target(config_dir: str, link: str) -> str | None:
    """Where ``link`` resolves to, as a path inside the archive, or None when
    that is outside every state path. A state path that is itself a link (a
    data folder on another disk) counts as the place it points to."""
    target = os.path.realpath(link)
    root = os.path.realpath(config_dir)
    for name in _STATE_PATHS:
        allowed = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([allowed, target]) == allowed:
            rest = os.path.relpath(target, allowed)
            return name if rest == "." else posixpath.join(name, *rest.split(os.sep))
    return None


def _linked_state(config_dir: str) -> dict[str, str]:
    """State paths that are themselves links, mapped to what they resolve to."""
    linked = {}
    for name in _STATE_PATHS:
        source = os.path.join(config_dir, name)
        if os.path.islink(source) and os.path.exists(source):
            linked[name] = os.path.realpath(source)
    return linked


def create_backup(config_dir: str) -> str:
    """Write a gzip tarball of the present state paths and return its path."""
    backup_dir = os.path.join(config_dir, _BACKUP_SUBDIR)
    os.makedirs(backup_dir, exist_ok=True)

    def skip_outside_links(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if not info.issym():
            return info
        inside = _archive_target(config_dir, os.path.join(config_dir, info.name))
        if inside is None:
            _LOGGER.warning("Backup skipped %s: it is a link to outside Nova's data", info.name)
            return None
        if os.path.isabs(info.linkname):
            # Restore refuses a link stored by its full path; store it
            # relative to its own folder so it points at the same file.
            info.linkname = posixpath.relpath(inside, posixpath.dirname(info.name) or ".")
        return info

    stamp = time.strftime("%Y%m%d-%H%M%S")

    # Written under a temporary name (owner-only from creation, and not
    # matched by the restore glob), then moved into place only on success.
    fd, tmp = tempfile.mkstemp(prefix=".nova-state-", suffix=".tmp", dir=backup_dir)
    try:
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            linked = _linked_state(config_dir)
            for name in _STATE_PATHS:
                source = os.path.join(config_dir, name)
                if os.path.exists(source):
                    # A state path that is a link is backed up as what it
                    # points to, not as the link.
                    tar.add(linked.get(name, source), arcname=name, filter=skip_outside_links)

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

    linked = _linked_state(config_dir)
    with tarfile.open(path, "r:gz") as tar:
        if not linked:
            tar.extractall(config_dir, filter="data")
            return path
        # A state path that is a link here is restored into the place it
        # points to; the data filter is then applied to that place.
        members = tar.getmembers()
        tar.extractall(config_dir, filter="data", members=[
            m for m in members if _top(m.name) not in linked])
        for name, target in linked.items():
            if os.path.isdir(target):
                # Into the folder itself, so nothing can land beside it.
                dest = target
                moved = [_moved(m, name + "/", "") for m in members
                         if m.name.startswith(name + "/")]
            else:
                dest = os.path.dirname(target)
                moved = [_moved(m, name, os.path.basename(target)) for m in members
                         if m.name == name]
            if moved:
                tar.extractall(dest, filter="data", members=moved)
    return path


def _top(member_name: str) -> str:
    return member_name.split("/", 1)[0]


def _moved(member: tarfile.TarInfo, old: str, new: str) -> tarfile.TarInfo:
    """``member`` with its leading ``old`` swapped for ``new`` (a hard link's
    target too, so it still names the same member)."""
    def swap(name: str) -> str:
        return new + name[len(old):] if name.startswith(old) else name
    changes = {"name": swap(member.name)}
    if member.islnk():
        changes["linkname"] = swap(member.linkname)
    return member.replace(**changes, deep=False)
