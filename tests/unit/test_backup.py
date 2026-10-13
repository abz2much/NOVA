"""Acceptance tests for the Nova state backup/restore module, written from
its behaviour spec (archive layout, permissions, archive selection,
extraction safety and the documented quirks)."""
import importlib.util
import io
import os
import stat
import tarfile
import time
from pathlib import Path

import pytest

STAMP_FORMAT = "%Y%m%d-%H%M%S"


@pytest.fixture
def bk(load):
    return load("backup")


@pytest.fixture
def fixed_stamp(monkeypatch):
    """Freeze the timestamp and record the format strings asked for."""
    seen = []

    def fake(fmt, *rest):
        seen.append((fmt, rest))
        return "20300102-030405"

    monkeypatch.setattr(time, "strftime", fake)
    return seen


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _populate(root: Path) -> None:
    _write(root / "nova" / "store.db", "STORE")
    _write(root / "nova" / "config.json", '{"k": 1}')
    _write(root / "nova" / "docs" / "note.md", "NOTE")
    _write(root / "nova.db", "KEYWORDS")
    _write(root / "nova_memory" / "index.bin", "VECTORS")
    _write(root / "nova_persona.txt", "PERSONA")
    _write(root / "nova_routines.yaml", "routines: []")


def _names(archive: str) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


def _make_tgz(path: Path, members) -> Path:
    """members: iterable of (name, bytes | None, mode, linkname)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name, data, mode, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                tar.addfile(info)
            else:
                info.size = len(data)
                info.mode = mode
                tar.addfile(info, io.BytesIO(data))
    return path


# ── module contract ──────────────────────────────────────────────────────────

def test_constants_have_exact_values(bk):
    assert bk._STATE_PATHS == [
        "nova", "nova.db", "nova_memory", "nova_persona.txt", "nova_routines.yaml"]
    assert isinstance(bk._STATE_PATHS, list)
    assert bk._BACKUP_SUBDIR == "nova_backups"


def test_module_loads_standalone_without_home_assistant():
    path = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "backup.py"
    spec = importlib.util.spec_from_file_location("standalone_backup_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.create_backup) and callable(mod.restore_backup)


def test_restore_archive_argument_defaults_to_empty_string(bk):
    import inspect
    sig = inspect.signature(bk.restore_backup)
    assert list(sig.parameters) == ["config_dir", "archive"]
    assert sig.parameters["archive"].default == ""
    assert list(inspect.signature(bk.create_backup).parameters) == ["config_dir"]


# ── create_backup ────────────────────────────────────────────────────────────

def test_create_names_archive_from_local_timestamp(bk, tmp_path, fixed_stamp):
    out = bk.create_backup(str(tmp_path))
    assert out == os.path.join(str(tmp_path), "nova_backups",
                               "nova-state-20300102-030405.tar.gz")
    assert fixed_stamp == [(STAMP_FORMAT, ())]
    assert os.path.isfile(out)


def test_create_real_timestamp_shape(bk, tmp_path):
    out = os.path.basename(bk.create_backup(str(tmp_path)))
    assert out.startswith("nova-state-") and out.endswith(".tar.gz")
    stamp = out[len("nova-state-"):-len(".tar.gz")]
    time.strptime(stamp, STAMP_FORMAT)


def test_create_returns_unnormalised_join(bk, tmp_path, fixed_stamp):
    cfg = str(tmp_path) + "/"
    out = bk.create_backup(cfg)
    assert out == os.path.join(cfg, "nova_backups", "nova-state-20300102-030405.tar.gz")


def test_create_makes_backup_folder_and_tolerates_existing(bk, tmp_path, fixed_stamp):
    assert not (tmp_path / "nova_backups").exists()
    bk.create_backup(str(tmp_path))
    assert (tmp_path / "nova_backups").is_dir()
    _write(tmp_path / "nova_backups" / "keep.txt", "x")
    bk.create_backup(str(tmp_path))
    assert (tmp_path / "nova_backups" / "keep.txt").read_text() == "x"


def test_create_creates_missing_config_dir(bk, tmp_path, fixed_stamp):
    cfg = tmp_path / "does" / "not" / "exist"
    out = bk.create_backup(str(cfg))
    assert cfg.is_dir() and os.path.isfile(out)
    assert _names(out) == []


def test_archive_is_owner_only(bk, tmp_path, fixed_stamp):
    _populate(tmp_path)
    out = bk.create_backup(str(tmp_path))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_archive_owner_only_even_with_permissive_umask(bk, tmp_path, fixed_stamp):
    old = os.umask(0)
    try:
        out = bk.create_backup(str(tmp_path))
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_same_second_archive_is_overwritten_and_tightened(bk, tmp_path, fixed_stamp):
    target = _write(tmp_path / "nova_backups" / "nova-state-20300102-030405.tar.gz",
                    "not an archive at all " * 50)
    os.chmod(target, 0o644)
    _write(tmp_path / "nova.db", "FRESH")
    out = bk.create_backup(str(tmp_path))
    assert out == str(target)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert _names(out) == ["nova.db"]
    assert len(list((tmp_path / "nova_backups").iterdir())) == 1


def test_archive_holds_all_state_paths_with_relative_names(bk, tmp_path, fixed_stamp):
    _populate(tmp_path)
    out = bk.create_backup(str(tmp_path))
    names = _names(out)
    assert set(names) == {
        "nova", "nova/store.db", "nova/config.json", "nova/docs", "nova/docs/note.md",
        "nova.db", "nova_memory", "nova_memory/index.bin",
        "nova_persona.txt", "nova_routines.yaml",
    }
    assert not any(n.startswith("/") or str(tmp_path) in n for n in names)


def test_archive_member_order_follows_state_list(bk, tmp_path, fixed_stamp):
    _populate(tmp_path)
    names = _names(bk.create_backup(str(tmp_path)))
    tops = []
    for n in names:
        top = n.split("/")[0]
        if not tops or tops[-1] != top:
            tops.append(top)
    assert tops == bk._STATE_PATHS
    # within a directory, entries come in sorted order
    nova_kids = [n for n in names if n.startswith("nova/") and n.count("/") == 1]
    assert nova_kids == sorted(nova_kids)


def test_archive_contents_round_trip_byte_for_byte(bk, tmp_path, fixed_stamp):
    _populate(tmp_path)
    out = bk.create_backup(str(tmp_path))
    with tarfile.open(out, "r:gz") as tar:
        assert tar.extractfile("nova/docs/note.md").read() == b"NOTE"
        assert tar.extractfile("nova_routines.yaml").read() == b"routines: []"


def test_archive_excludes_everything_else(bk, tmp_path, fixed_stamp):
    _populate(tmp_path)
    _write(tmp_path / ".nova_learned.json", "{}")
    _write(tmp_path / "configuration.yaml", "x: 1")
    _write(tmp_path / "secrets.yaml", "pw: y")
    _write(tmp_path / "nova_extra.txt", "z")
    _write(tmp_path / "nova_backups" / "nova-state-19990101-000000.tar.gz", "old")
    names = _names(bk.create_backup(str(tmp_path)))
    for bad in (".nova_learned.json", "configuration.yaml", "secrets.yaml",
                "nova_extra.txt", "nova_backups"):
        assert not any(n == bad or n.startswith(bad + "/") for n in names)


def test_missing_state_paths_are_skipped(bk, tmp_path, fixed_stamp):
    _write(tmp_path / "nova_persona.txt", "P")
    assert _names(bk.create_backup(str(tmp_path))) == ["nova_persona.txt"]


def test_empty_state_still_writes_valid_archive(bk, tmp_path, fixed_stamp):
    out = bk.create_backup(str(tmp_path))
    assert _names(out) == []


def test_state_path_may_be_file_or_directory(bk, tmp_path, fixed_stamp):
    _write(tmp_path / "nova", "a plain file named nova")
    (tmp_path / "nova.db").mkdir()
    _write(tmp_path / "nova.db" / "inner", "i")
    names = _names(bk.create_backup(str(tmp_path)))
    assert names == ["nova", "nova.db", "nova.db/inner"]


def test_top_level_symlink_archived_as_link_only(bk, tmp_path, fixed_stamp):
    real = tmp_path / "elsewhere"
    _write(real / "big.db", "DATA")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    os.symlink(real, cfg / "nova")
    out = bk.create_backup(str(cfg))
    with tarfile.open(out, "r:gz") as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == ["nova"]
    assert members[0].issym() and members[0].linkname == str(real)


def test_dangling_top_level_symlink_is_skipped(bk, tmp_path, fixed_stamp):
    os.symlink(tmp_path / "gone", tmp_path / "nova.db")
    assert _names(bk.create_backup(str(tmp_path))) == []


def test_nested_symlink_kept_as_symlink(bk, tmp_path, fixed_stamp):
    _write(tmp_path / "nova" / "real.txt", "R")
    os.symlink("real.txt", tmp_path / "nova" / "alias.txt")
    out = bk.create_backup(str(tmp_path))
    with tarfile.open(out, "r:gz") as tar:
        m = tar.getmember("nova/alias.txt")
    assert m.issym() and m.linkname == "real.txt"


def test_write_failure_propagates_and_leaves_partial_file(bk, tmp_path, fixed_stamp, monkeypatch):
    _populate(tmp_path)

    class Boom(OSError):
        pass

    def broken_add(self, *a, **k):
        raise Boom("cannot read")

    monkeypatch.setattr(tarfile.TarFile, "add", broken_add)
    with pytest.raises(Boom):
        bk.create_backup(str(tmp_path))
    left = tmp_path / "nova_backups" / "nova-state-20300102-030405.tar.gz"
    assert left.exists()
    assert stat.S_IMODE(os.stat(left).st_mode) == 0o600


# ── restore_backup: choosing the archive ────────────────────────────────────

def test_restore_without_archives_raises_with_folder_in_message(bk, tmp_path):
    with pytest.raises(FileNotFoundError) as err:
        bk.restore_backup(str(tmp_path))
    assert str(err.value) == "no Nova backups found in " + os.path.join(
        str(tmp_path), "nova_backups")
    assert not (tmp_path / "nova_backups").exists()


def test_restore_with_empty_backup_folder_raises(bk, tmp_path):
    (tmp_path / "nova_backups").mkdir()
    _write(tmp_path / "nova_backups" / "other.tar.gz", "x")
    _write(tmp_path / "nova_backups" / "nova-state-1.tgz", "x")
    with pytest.raises(FileNotFoundError):
        bk.restore_backup(str(tmp_path))


def test_restore_picks_greatest_name_not_newest_mtime(bk, tmp_path):
    bdir = tmp_path / "nova_backups"
    later_name = _make_tgz(bdir / "nova-state-20260101-000000.tar.gz",
                           [("pick.txt", b"later-name", 0o644, None)])
    older_name = _make_tgz(bdir / "nova-state-20250101-000000.tar.gz",
                           [("pick.txt", b"older-name", 0o644, None)])
    os.utime(later_name, (1, 1))
    os.utime(older_name, None)
    used = bk.restore_backup(str(tmp_path))
    assert used == str(later_name)
    assert (tmp_path / "pick.txt").read_text() == "later-name"


def test_restore_hand_named_archive_outsorts_timestamps(bk, tmp_path):
    bdir = tmp_path / "nova_backups"
    _make_tgz(bdir / "nova-state-20991231-235959.tar.gz", [("w.txt", b"stamp", 0o644, None)])
    odd = _make_tgz(bdir / "nova-state-zzz.tar.gz", [("w.txt", b"odd", 0o644, None)])
    assert bk.restore_backup(str(tmp_path)) == str(odd)
    assert (tmp_path / "w.txt").read_text() == "odd"


def test_restore_none_means_latest(bk, tmp_path):
    arc = _make_tgz(tmp_path / "nova_backups" / "nova-state-20200101-000000.tar.gz",
                    [("n.txt", b"1", 0o644, None)])
    assert bk.restore_backup(str(tmp_path), None) == str(arc)


def test_restore_relative_name_resolves_in_backup_folder(bk, tmp_path):
    bdir = tmp_path / "nova_backups"
    _make_tgz(bdir / "nova-state-20990101-000000.tar.gz", [("f.txt", b"newest", 0o644, None)])
    chosen = _make_tgz(bdir / "custom-name.tar.gz", [("f.txt", b"chosen", 0o644, None)])
    used = bk.restore_backup(str(tmp_path), "custom-name.tar.gz")
    assert used == os.path.join(str(bdir), "custom-name.tar.gz") == str(chosen)
    assert (tmp_path / "f.txt").read_text() == "chosen"


def test_restore_relative_subpath_joined_literally(bk, tmp_path):
    _make_tgz(tmp_path / "nova_backups" / "sub" / "a.tar.gz", [("s.txt", b"s", 0o644, None)])
    used = bk.restore_backup(str(tmp_path), "sub/a.tar.gz")
    assert used == os.path.join(str(tmp_path), "nova_backups", "sub/a.tar.gz")


def test_restore_relative_dotdot_escapes_backup_folder(bk, tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "nova_backups").mkdir(parents=True)
    _make_tgz(cfg / "outside.tar.gz", [("e.txt", b"escaped", 0o644, None)])
    used = bk.restore_backup(str(cfg), "../outside.tar.gz")
    assert used == os.path.join(str(cfg), "nova_backups", "../outside.tar.gz")
    assert (cfg / "e.txt").read_text() == "escaped"


def test_restore_absolute_path_used_anywhere(bk, tmp_path):
    arc = _make_tgz(tmp_path / "far" / "away.tar.gz", [("abs.txt", b"A", 0o644, None)])
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    assert bk.restore_backup(str(cfg), str(arc)) == str(arc)
    assert (cfg / "abs.txt").read_text() == "A"


def test_restore_missing_named_archive_reports_resolved_path(bk, tmp_path):
    with pytest.raises(FileNotFoundError) as err:
        bk.restore_backup(str(tmp_path), "nope.tar.gz")
    expected = os.path.join(str(tmp_path), "nova_backups", "nope.tar.gz")
    assert err.value.args == (expected,)
    assert str(err.value) == expected


def test_restore_missing_absolute_archive_reports_it(bk, tmp_path):
    missing = str(tmp_path / "missing.tar.gz")
    with pytest.raises(FileNotFoundError) as err:
        bk.restore_backup(str(tmp_path), missing)
    assert str(err.value) == missing


# ── restore_backup: extraction ──────────────────────────────────────────────

def test_round_trip_restores_deleted_state(bk, tmp_path):
    _populate(tmp_path)
    made = bk.create_backup(str(tmp_path))
    for p in ("nova/store.db", "nova/docs/note.md", "nova.db", "nova_memory/index.bin",
              "nova_persona.txt"):
        os.remove(tmp_path / p)
    assert bk.restore_backup(str(tmp_path)) == made
    assert (tmp_path / "nova" / "store.db").read_text() == "STORE"
    assert (tmp_path / "nova" / "docs" / "note.md").read_text() == "NOTE"
    assert (tmp_path / "nova.db").read_text() == "KEYWORDS"
    assert (tmp_path / "nova_memory" / "index.bin").read_text() == "VECTORS"
    assert (tmp_path / "nova_persona.txt").read_text() == "PERSONA"


def test_restore_overwrites_but_never_deletes(bk, tmp_path):
    _write(tmp_path / "nova" / "a.txt", "old-a")
    made = bk.create_backup(str(tmp_path))
    _write(tmp_path / "nova" / "a.txt", "changed-a")
    _write(tmp_path / "nova" / "stray.txt", "stray")
    bk.restore_backup(str(tmp_path), made)
    assert (tmp_path / "nova" / "a.txt").read_text() == "old-a"
    assert (tmp_path / "nova" / "stray.txt").read_text() == "stray"


def test_restore_writes_members_outside_state_list(bk, tmp_path):
    arc = _make_tgz(tmp_path / "nova_backups" / "nova-state-20200101-000000.tar.gz",
                    [("configuration.yaml", b"replaced: true", 0o644, None)])
    _write(tmp_path / "configuration.yaml", "original: true")
    bk.restore_backup(str(tmp_path), str(arc))
    assert (tmp_path / "configuration.yaml").read_text() == "replaced: true"


def test_restore_strips_leading_slash(bk, tmp_path):
    arc = _make_tgz(tmp_path / "x" / "a.tar.gz", [("/rooted.txt", b"r", 0o644, None)])
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    bk.restore_backup(str(cfg), str(arc))
    assert (cfg / "rooted.txt").read_text() == "r"


def test_restore_refuses_member_outside_destination(bk, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    arc = _make_tgz(tmp_path / "x" / "a.tar.gz", [
        ("first.txt", b"1", 0o644, None),
        ("../escape.txt", b"bad", 0o644, None),
    ])
    with pytest.raises(tarfile.OutsideDestinationError):
        bk.restore_backup(str(cfg), str(arc))
    assert not (tmp_path / "escape.txt").exists()
    assert (cfg / "first.txt").read_text() == "1"   # no rollback


def test_restore_refuses_absolute_symlink_member(bk, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    arc = _make_tgz(tmp_path / "x" / "a.tar.gz", [("link", None, 0o777, "/etc/hostname")])
    with pytest.raises(tarfile.AbsoluteLinkError):
        bk.restore_backup(str(cfg), str(arc))
    assert not os.path.lexists(cfg / "link")


def test_backup_with_absolute_symlink_cannot_be_restored(bk, tmp_path, fixed_stamp):
    real = tmp_path / "elsewhere"
    _write(real / "data.db", "D")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    os.symlink(real, cfg / "nova")
    _write(cfg / "nova.db", "K")
    made = bk.create_backup(str(cfg))
    os.remove(cfg / "nova")
    with pytest.raises(tarfile.AbsoluteLinkError):
        bk.restore_backup(str(cfg), made)


def test_restore_clears_group_other_write(bk, tmp_path):
    arc = _make_tgz(tmp_path / "x" / "a.tar.gz", [
        ("wide.txt", b"w", 0o666, None),
        ("tight.txt", b"t", 0o600, None),
    ])
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    bk.restore_backup(str(cfg), str(arc))
    assert stat.S_IMODE(os.stat(cfg / "wide.txt").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(cfg / "tight.txt").st_mode) == 0o600


def test_restore_uses_data_filter(bk, tmp_path, monkeypatch):
    arc = _make_tgz(tmp_path / "x" / "a.tar.gz", [("f.txt", b"f", 0o644, None)])
    calls = []
    real = tarfile.TarFile.extractall

    def spy(self, path=".", *a, **k):
        calls.append((str(path), a, dict(k)))
        return real(self, path, *a, **k)

    monkeypatch.setattr(tarfile.TarFile, "extractall", spy)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    bk.restore_backup(str(cfg), str(arc))
    assert calls == [(str(cfg), (), {"filter": "data"})]


def test_restore_falls_back_without_filter_on_type_error(bk, tmp_path, monkeypatch):
    arc = _make_tgz(tmp_path / "x" / "a.tar.gz", [("f.txt", b"legacy", 0o644, None)])
    calls = []
    real = tarfile.TarFile.extractall

    def old_python(self, path=".", *a, **k):
        calls.append(dict(k))
        if "filter" in k:
            raise TypeError("unexpected keyword argument 'filter'")
        return real(self, path, *a, filter="fully_trusted")

    monkeypatch.setattr(tarfile.TarFile, "extractall", old_python)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    assert bk.restore_backup(str(cfg), str(arc)) == str(arc)
    assert calls == [{"filter": "data"}, {}]
    assert (cfg / "f.txt").read_text() == "legacy"


def test_restore_non_gzip_archive_raises_read_error(bk, tmp_path):
    bad = _write(tmp_path / "nova_backups" / "nova-state-20200101-000000.tar.gz", "junk" * 40)
    with pytest.raises(tarfile.ReadError):
        bk.restore_backup(str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nova_backups"]
    assert bad.exists()


def test_restore_directory_as_archive_raises_os_error(bk, tmp_path):
    (tmp_path / "nova_backups" / "dir.tar.gz").mkdir(parents=True)
    with pytest.raises(OSError):
        bk.restore_backup(str(tmp_path), "dir.tar.gz")


def test_partial_backup_is_chosen_as_latest(bk, tmp_path, monkeypatch):
    _populate(tmp_path)
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "20200101-000000")
    good = bk.create_backup(str(tmp_path))

    def broken_add(self, *a, **k):
        raise OSError("disk went away")

    with monkeypatch.context() as m:
        m.setattr(time, "strftime", lambda fmt, *a: "20200101-000001")
        m.setattr(tarfile.TarFile, "add", broken_add)
        with pytest.raises(OSError):
            bk.create_backup(str(tmp_path))
    with pytest.raises(tarfile.TarError):
        bk.restore_backup(str(tmp_path))
    assert bk.restore_backup(str(tmp_path), os.path.basename(good)) == good
