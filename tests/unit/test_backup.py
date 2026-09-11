"""Backup/restore of Nova state — archive, wipe, restore round-trip."""
import importlib.util
import os
from pathlib import Path

import pytest

COMP = Path(__file__).resolve().parents[2] / "custom_components" / "nova"


def _load_backup():
    spec = importlib.util.spec_from_file_location("nova_backup", COMP / "backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backup = _load_backup()


def test_backup_restore_roundtrip(tmp_path):
    cfg = str(tmp_path)
    os.makedirs(os.path.join(cfg, "nova"))
    open(os.path.join(cfg, "nova", "knowledge.db"), "w").write("KNOW")
    open(os.path.join(cfg, "nova.db"), "w").write("FTS")
    os.makedirs(os.path.join(cfg, "nova_memory"))
    open(os.path.join(cfg, "nova_memory", "chroma.sqlite3"), "w").write("CHROMA")

    arc = backup.create_backup(cfg)
    assert os.path.exists(arc) and arc.endswith(".tar.gz")

    # wipe, then restore the latest backup
    os.remove(os.path.join(cfg, "nova", "knowledge.db"))
    os.remove(os.path.join(cfg, "nova.db"))
    used = backup.restore_backup(cfg)
    assert used == arc
    assert open(os.path.join(cfg, "nova", "knowledge.db")).read() == "KNOW"
    assert open(os.path.join(cfg, "nova.db")).read() == "FTS"
    assert open(os.path.join(cfg, "nova_memory", "chroma.sqlite3")).read() == "CHROMA"


def test_restore_no_backup_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.restore_backup(str(tmp_path))
