"""8.49.11 regression tests for backup and restore. Each one fails on 8.49.10."""
import io
import logging
import os
import stat
import tarfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def bk(load):
    return load("backup")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _make_tgz(path: Path, name: str, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return path


def test_failed_backup_leaves_no_file_and_keeps_existing_archive(bk, tmp_path, monkeypatch):
    # 8.49.10 wrote straight to the final name and left a partial archive,
    # which then overwrote (same second) or outranked (later) the good one.
    _write(tmp_path / "nova.db", "GOOD")
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "20300102-030405")
    good = bk.create_backup(str(tmp_path))
    good_bytes = Path(good).read_bytes()

    def broken_add(self, *a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(tarfile.TarFile, "add", broken_add)
    with pytest.raises(OSError, match="disk went away"):
        bk.create_backup(str(tmp_path))
    assert os.listdir(tmp_path / "nova_backups") == [os.path.basename(good)]
    assert Path(good).read_bytes() == good_bytes


def test_learned_aliases_and_ignore_rules_are_backed_up(bk, tmp_path):
    # 8.49.10 left both config-root files out of the archive.
    _write(tmp_path / ".nova_learned.json", '{"alias": "x"}')
    _write(tmp_path / ".nova_ignore_rules.json", '{"rules": []}')
    made = bk.create_backup(str(tmp_path))
    os.remove(tmp_path / ".nova_learned.json")
    os.remove(tmp_path / ".nova_ignore_rules.json")
    bk.restore_backup(str(tmp_path), made)
    assert (tmp_path / ".nova_learned.json").read_text() == '{"alias": "x"}'
    assert (tmp_path / ".nova_ignore_rules.json").read_text() == '{"rules": []}'


def test_type_error_is_not_retried_without_the_data_filter(bk, tmp_path, monkeypatch):
    # 8.49.10 retried extractall with no filter after any TypeError.
    arc = _make_tgz(tmp_path / "x" / "a.tar.gz", "f.txt", b"f")
    calls = []

    def raising(self, path=".", *a, **k):
        calls.append(dict(k))
        raise TypeError("internal error")

    monkeypatch.setattr(tarfile.TarFile, "extractall", raising)
    with pytest.raises(TypeError, match="internal error"):
        bk.restore_backup(str(tmp_path), str(arc))
    assert calls == [{"filter": "data"}]


def test_same_second_backups_get_distinct_names(bk, tmp_path, monkeypatch):
    # 8.49.10 gave both the same name, so the second replaced the first.
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "20300102-030405")
    _write(tmp_path / "nova.db", "FIRST")
    first = bk.create_backup(str(tmp_path))
    _write(tmp_path / "nova.db", "SECOND")
    second = bk.create_backup(str(tmp_path))
    assert first != second
    with tarfile.open(first, "r:gz") as tar:
        assert tar.extractfile("nova.db").read() == b"FIRST"
    with tarfile.open(second, "r:gz") as tar:
        assert tar.extractfile("nova.db").read() == b"SECOND"
    assert os.path.basename(second).startswith("nova-state-20300102-030405")
    assert second.endswith(".tar.gz")


def test_restore_picks_newest_by_modification_time(bk, tmp_path):
    # 8.49.10 picked the greatest name, not the newest file.
    bdir = tmp_path / "nova_backups"
    older = _make_tgz(bdir / "nova-state-20991231-235959.tar.gz", "pick.txt", b"older")
    newer = _make_tgz(bdir / "nova-state-20200101-000000.tar.gz", "pick.txt", b"newer")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    assert bk.restore_backup(str(tmp_path)) == str(newer)
    assert (tmp_path / "pick.txt").read_text() == "newer"


def test_symlink_outside_nova_data_is_skipped_with_warning(bk, tmp_path, caplog):
    # 8.49.10 archived any symlink as a link, including one to outside data.
    outside = _write(tmp_path / "outside" / "secret.txt", "SECRET")
    inside = _write(tmp_path / "cfg" / "nova" / "real.txt", "REAL")
    cfg = tmp_path / "cfg"
    os.symlink(outside, cfg / "nova" / "out_link")
    os.symlink("real.txt", cfg / "nova" / "in_link")
    with caplog.at_level(logging.WARNING):
        made = bk.create_backup(str(cfg))
    with tarfile.open(made, "r:gz") as tar:
        names = tar.getnames()
        assert tar.getmember("nova/in_link").issym()
    assert "nova/out_link" not in names
    assert "nova/real.txt" in names
    assert any("nova/out_link" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)
    assert inside.read_text() == "REAL"
