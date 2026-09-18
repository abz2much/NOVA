"""Action Audit Log coverage for __init__.py's directly-registered HA
services that a unit fake can't reach cleanly (they're closures over a real
config entry / hass.config.path()): nova.backup, nova.restore, and
nova.test_notify. Companion to test_action_log_websocket.py.

backup/restore do real (small) file I/O under hass.config.path() -- PHACC's
default testing_config dir is NOT per-test-isolated (it's a fixed path on
disk shared across runs), so every test here repoints hass.config.path at
tmp_path first to stay hermetic and avoid leaving real tar.gz files behind
in the venv.
"""
from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry
from .test_wiring_smoke import _make_entry

DOMAIN = "nova"


async def _setup_nova(hass, tmp_path=None, monkeypatch=None, options=None):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "persistent_notification", {})
    if tmp_path is not None and monkeypatch is not None:
        config_dir = str(tmp_path / "ha_config")
        monkeypatch.setattr(hass.config, "path",
                             lambda *p, _base=config_dir: __import__("os").path.join(_base, *p))
        # nova_config is a process-global singleton whose configure() only
        # repoints CONFIG_PATH, never resets _cache/_loaded -- without this,
        # a value cached from an earlier PHACC test in this same session
        # (different hass, different tmp_path) leaks into this one's reads.
        from custom_components.nova import nova_config as _nc
        monkeypatch.setattr(_nc, "_cache", {})
        monkeypatch.setattr(_nc, "_loaded", False)
    entry = _make_entry()
    if options:
        entry = MockConfigEntry(domain=entry.domain, data=entry.data, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_backup_service_logs_one_accepted_action(hass, tmp_path, monkeypatch):
    from custom_components.nova import action_log

    await _setup_nova(hass, tmp_path, monkeypatch)
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    await hass.services.async_call(DOMAIN, "backup", {}, blocking=True)
    await hass.async_block_till_done()

    page = action_log.page_requests(limit=10, db_path=db_path)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert row["action"] == "backup"
    assert row["source"] == "ha_service"
    assert row["targets"][0]["execution_result"] == "accepted"


async def test_restore_service_logs_one_accepted_action(hass, tmp_path, monkeypatch):
    from custom_components.nova import action_log

    await _setup_nova(hass, tmp_path, monkeypatch)
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    # A real archive must exist first, or restore_backup raises before the
    # action_log row could ever reach "accepted" -- create one for real via
    # the backup service.
    await hass.services.async_call(DOMAIN, "backup", {}, blocking=True)
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, "restore", {}, blocking=True)
    await hass.async_block_till_done()

    page = action_log.page_requests(limit=10, db_path=db_path)
    restores = [r for r in page["requests"] if r["action"] == "restore"]
    assert len(restores) == 1
    assert restores[0]["targets"][0]["execution_result"] == "accepted"


async def test_restore_service_logs_failure_when_no_backup_exists(hass, tmp_path, monkeypatch):
    from custom_components.nova import action_log

    await _setup_nova(hass, tmp_path, monkeypatch)
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    try:
        await hass.services.async_call(DOMAIN, "restore", {}, blocking=True)
        await hass.async_block_till_done()
    except Exception:
        pass  # restore_backup legitimately raises with nothing to restore

    page = action_log.page_requests(limit=10, db_path=db_path)
    restores = [r for r in page["requests"] if r["action"] == "restore"]
    assert len(restores) == 1
    assert restores[0]["targets"][0]["execution_result"] == "failed"


async def test_test_notify_service_logs_one_accepted_action(hass, tmp_path, monkeypatch):
    from custom_components.nova import action_log

    calls = []
    async def fake_notify(call):
        calls.append(call.data)
    hass.services.async_register("notify", "mobile_app_test", fake_notify)

    await _setup_nova(hass, tmp_path, monkeypatch,
                       options={"notify_service": "notify.mobile_app_test"})
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    await hass.services.async_call(DOMAIN, "test_notify", {}, blocking=True)
    await hass.async_block_till_done()

    assert len(calls) == 1
    page = action_log.page_requests(limit=10, db_path=db_path)
    rows = [r for r in page["requests"] if r["action"] == "test_notify"]
    assert len(rows) == 1
    assert rows[0]["targets"][0]["execution_result"] == "accepted"


async def test_test_notify_service_creates_no_row_when_unconfigured(hass, tmp_path, monkeypatch):
    from custom_components.nova import action_log

    await _setup_nova(hass, tmp_path, monkeypatch)
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    await hass.services.async_call(DOMAIN, "test_notify", {}, blocking=True)
    await hass.async_block_till_done()

    page = action_log.page_requests(limit=10, db_path=db_path)
    assert [r for r in page["requests"] if r["action"] == "test_notify"] == []
