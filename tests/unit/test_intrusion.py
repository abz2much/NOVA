"""Tests for intrusion snapshots + false-alarm call-off (v6.68.0). Covers the
call-off suppression window (safety-relevant: a call-off must actually suppress
escalation), false-alarm recording, snapshot capture, and the status shape."""
import pathlib
import time

import pytest


@pytest.fixture
def intr(load, tmp_path, monkeypatch):
    m = load("intrusion")
    # isolate snapshot dir + reset module state each test
    monkeypatch.setattr(m, "SNAPSHOT_DIR", str(tmp_path / "snaps"))
    m._called_off_until = 0.0
    m._last_snapshot = {}
    m._false_alarms = []
    return m


# ── call-off suppression ─────────────────────────────────────────────────────

def test_not_called_off_by_default(intr):
    assert intr.is_called_off() is False


def test_dismiss_suppresses_escalation(intr):
    res = intr.dismiss_intrusion("it was the cat")
    assert res["ok"] is True
    assert res["suppressed_seconds"] > 0
    assert intr.is_called_off() is True          # escalation now suppressed


def test_calloff_expires(intr, monkeypatch):
    intr.dismiss_intrusion()
    assert intr.is_called_off() is True
    # jump past the cooldown (capture real now first to avoid recursion)
    future = time.time() + intr._CALLOFF_COOLDOWN + 5
    monkeypatch.setattr(intr.time, "time", lambda: future)
    assert intr.is_called_off() is False


def test_clear_calloff_resets(intr):
    intr.dismiss_intrusion()
    assert intr.is_called_off() is True
    intr.clear_calloff()
    assert intr.is_called_off() is False


# ── false-alarm recording ────────────────────────────────────────────────────

def test_false_alarm_recorded(intr):
    intr.dismiss_intrusion("cat again")
    assert intr.false_alarm_count() == 1


def test_false_alarm_count_windowed(intr, monkeypatch):
    intr.dismiss_intrusion()
    # an old false alarm outside the window shouldn't count
    intr._false_alarms.insert(0, {"ts": int(time.time()) - 200000, "reason": "old"})
    assert intr.false_alarm_count(within_seconds=86400) == 1   # only the recent one


def test_false_alarm_list_capped(intr):
    for _ in range(60):
        intr.dismiss_intrusion()
    assert len(intr._false_alarms) <= 50


# ── snapshot capture ─────────────────────────────────────────────────────────

async def test_capture_snapshot_writes_file(intr, tmp_path):
    class _Img:
        content = b"\xff\xd8\xff\xe0jpegbytes"
    class _Hass:
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)
    hass = _Hass()

    import sys, types
    cam_mod = types.ModuleType("homeassistant.components.camera")
    async def _get_image(h, entity, timeout=10):
        return _Img()
    cam_mod.async_get_image = _get_image
    sys.modules["homeassistant.components.camera"] = cam_mod
    try:
        info = await intr.capture_snapshot(hass, "camera.dining_room")
    finally:
        sys.modules.pop("homeassistant.components.camera", None)

    assert info is not None
    assert info["camera"] == "camera.dining_room"
    # No unauthenticated URL is ever produced (v7.102.0) — /config/www's
    # /local/... path is served with no auth at all; snapshots reach the
    # panel only via the authenticated nova/intrusion websocket command.
    assert "url" not in info
    import os
    assert os.path.exists(info["path"])
    assert intr.last_snapshot()["camera"] == "camera.dining_room"


def test_snapshot_dir_is_not_under_config_www(load):
    """The real (unpatched) SNAPSHOT_DIR must never be under /config/www —
    Home Assistant serves that whole tree at /local/... with no
    authentication at all. Checked against the module's own source constant,
    not a test's redirected path, so this can't pass by accident."""
    m = load("intrusion")
    assert "/www/" not in m.SNAPSHOT_DIR
    src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    assert 'SNAPSHOT_DIR = "/config/nova/intrusion"' in src


async def test_capture_snapshot_no_entity_returns_none(intr):
    class _Hass: ...
    assert await intr.capture_snapshot(_Hass(), "") is None


async def test_capture_snapshot_never_raises(intr):
    class _Hass:
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)
    import sys, types
    cam_mod = types.ModuleType("homeassistant.components.camera")
    async def _boom(h, entity, timeout=10):
        raise RuntimeError("camera offline")
    cam_mod.async_get_image = _boom
    sys.modules["homeassistant.components.camera"] = cam_mod
    try:
        assert await intr.capture_snapshot(_Hass(), "camera.x") is None   # no crash
    finally:
        sys.modules.pop("homeassistant.components.camera", None)


# ── base64 read-back (panel delivery path) ────────────────────────────────────

async def test_get_snapshot_b64_roundtrip(intr):
    import base64, os
    class _Hass:
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)
    os.makedirs(intr.SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(intr.SNAPSHOT_DIR, "x.jpg")
    with open(path, "wb") as f:
        f.write(b"jpegbytes")
    out = await intr.get_snapshot_b64(_Hass(), path)
    assert base64.b64decode(out) == b"jpegbytes"


async def test_get_snapshot_b64_missing_file_returns_none(intr):
    class _Hass:
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)
    assert await intr.get_snapshot_b64(_Hass(), "/does/not/exist.jpg") is None


async def test_get_snapshot_b64_refuses_path_outside_snapshot_dir(intr, tmp_path):
    """A path pointing outside SNAPSHOT_DIR must never be read, even though
    today every caller supplies paths from our own stored metadata — this is
    defense in depth against a future caller passing something untrusted."""
    import os
    class _Hass:
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not yours")
    assert await intr.get_snapshot_b64(_Hass(), str(outside)) is None


# ── legacy snapshot migration ─────────────────────────────────────────────────

def test_migrate_legacy_snapshots_moves_files_and_repoints_log(intr, tmp_path, monkeypatch):
    import os
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "a.jpg").write_bytes(b"one")
    monkeypatch.setattr(intr, "_LEGACY_SNAPSHOT_DIR", str(legacy))
    intr._log = [{"id": "evt_1", "snapshot_path": str(legacy / "a.jpg")}]
    intr._log_loaded = True

    result = intr.migrate_legacy_snapshots()

    assert result == {"moved": 1, "skipped": 0}
    assert os.path.exists(os.path.join(intr.SNAPSHOT_DIR, "a.jpg"))
    assert not os.path.exists(str(legacy / "a.jpg"))          # moved, not copied
    assert intr._log[0]["snapshot_path"] == os.path.join(intr.SNAPSHOT_DIR, "a.jpg")


def test_migrate_legacy_snapshots_noop_when_nothing_legacy(intr, tmp_path, monkeypatch):
    monkeypatch.setattr(intr, "_LEGACY_SNAPSHOT_DIR", str(tmp_path / "never_existed"))
    assert intr.migrate_legacy_snapshots() == {"moved": 0, "skipped": 0}


def test_migrate_legacy_snapshots_does_not_overwrite_or_delete_existing_destination(
        intr, tmp_path, monkeypatch):
    """If a file with the same name already exists at the destination, the
    legacy copy is left in place — never overwritten, never deleted."""
    import os
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "a.jpg").write_bytes(b"legacy-bytes")
    monkeypatch.setattr(intr, "_LEGACY_SNAPSHOT_DIR", str(legacy))
    os.makedirs(intr.SNAPSHOT_DIR, exist_ok=True)
    with open(os.path.join(intr.SNAPSHOT_DIR, "a.jpg"), "wb") as f:
        f.write(b"already-here")

    result = intr.migrate_legacy_snapshots()

    assert result == {"moved": 0, "skipped": 1}
    assert (legacy / "a.jpg").exists()                         # not deleted
    with open(os.path.join(intr.SNAPSHOT_DIR, "a.jpg"), "rb") as f:
        assert f.read() == b"already-here"                     # not overwritten


# ── notification image (signed, short-lived, media_source-backed) ────────────

async def test_get_notification_image_url_no_media_dir_returns_none(intr):
    class _Config:
        media_dirs = {}
    class _Hass:
        config = _Config()
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)
    assert await intr.get_notification_image_url(_Hass(), "/some/path.jpg") is None


async def test_get_notification_image_url_missing_snapshot_path_returns_none(intr):
    class _Hass: ...
    assert await intr.get_notification_image_url(_Hass(), "") is None


async def test_get_notification_image_url_signs_and_schedules_cleanup(
        intr, tmp_path, monkeypatch):
    """The happy path: the private snapshot is copied into the configured
    local media directory under an unguessable name, the resulting /media/...
    path is signed (never the raw, unsigned path), and a cleanup callback is
    scheduled rather than the file being left indefinitely."""
    import os, sys, types

    media_root = tmp_path / "media"
    media_root.mkdir()
    src = tmp_path / "src.jpg"
    src.write_bytes(b"jpegbytes")

    class _Config:
        media_dirs = {"local": str(media_root)}
    class _Hass:
        config = _Config()
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)
    hass = _Hass()

    signed_calls = []
    def _fake_sign_path(h, path, expiry):
        signed_calls.append((path, expiry))
        return path + "?authSig=faketoken"

    scheduled = []
    def _fake_call_later(h, delay, cb):
        scheduled.append((delay, cb))
        return lambda: None

    auth_mod = types.ModuleType("homeassistant.components.http.auth")
    auth_mod.async_sign_path = _fake_sign_path
    event_mod = types.ModuleType("homeassistant.helpers.event")
    event_mod.async_call_later = _fake_call_later
    sys.modules["homeassistant.components.http.auth"] = auth_mod
    sys.modules["homeassistant.helpers.event"] = event_mod
    try:
        url = await intr.get_notification_image_url(hass, str(src))
    finally:
        sys.modules.pop("homeassistant.components.http.auth", None)
        sys.modules.pop("homeassistant.helpers.event", None)

    assert url is not None
    assert url.startswith("/media/local/nova_intrusion/")
    assert "?authSig=faketoken" in url
    assert len(signed_calls) == 1
    signed_path, _expiry = signed_calls[0]
    assert "?" not in signed_path                    # the UNSIGNED path was what got signed
    assert signed_path.startswith("/media/local/nova_intrusion/")
    # The copy actually exists on disk, under a name that isn't the source's.
    copied_files = os.listdir(str(media_root / "nova_intrusion"))
    assert len(copied_files) == 1
    assert copied_files[0] != "src.jpg"
    with open(str(media_root / "nova_intrusion" / copied_files[0]), "rb") as f:
        assert f.read() == b"jpegbytes"
    # Cleanup was scheduled, not skipped.
    assert len(scheduled) == 1


# ── status shape ─────────────────────────────────────────────────────────────

def test_status_shape(intr):
    st = intr.status()
    assert set(("last_snapshot", "called_off", "suppressed_for", "false_alarms_24h")) <= set(st)
    assert st["called_off"] is False


# ── agent tool registration ──────────────────────────────────────────────────

def test_dismiss_intrusion_tool_registered(load):
    agent = load("agent")
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert "dismiss_intrusion" in names
    assert "dismiss_intrusion" in agent._TOOL_MAP


# ── acknowledge (hold auto-escalation without cancelling) v6.69.0 ────────────

def test_acknowledge_holds_escalation(intr):
    assert intr.is_acknowledged() is False
    res = intr.acknowledge("I'm looking")
    assert res["ok"] is True
    assert intr.is_acknowledged() is True


def test_acknowledge_expires(intr, monkeypatch):
    intr.acknowledge()
    assert intr.is_acknowledged() is True
    future = time.time() + intr._ACK_WINDOW + 5
    monkeypatch.setattr(intr.time, "time", lambda: future)
    assert intr.is_acknowledged() is False


def test_acknowledge_is_not_calloff(intr):
    # acknowledging must NOT suppress evidence-based escalation (not a false alarm)
    intr.acknowledge()
    assert intr.is_acknowledged() is True
    assert intr.is_called_off() is False           # distinct states


def test_clear_resets_acknowledge(intr):
    intr.acknowledge()
    intr.clear_calloff()
    assert intr.is_acknowledged() is False


def test_status_includes_acknowledged(intr):
    st = intr.status()
    assert "acknowledged" in st and st["acknowledged"] is False


# ── acknowledge tool registration ────────────────────────────────────────────

def test_acknowledge_tool_registered(load):
    agent = load("agent")
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert "acknowledge_alert" in names
    assert "acknowledge_alert" in agent._TOOL_MAP
