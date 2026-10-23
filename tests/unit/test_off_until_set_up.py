"""Package watch, face recognition and departure reminders are off until
set up (8.41.0).

A fresh install starts with all three off. An install from before 8.41.0
that never saved one of them keeps the old default, written into its saved
setting once by the v8 to v9 migration, so nothing changes for it.
"""
import pytest

OLD = {"package_detection": True, "recognition_source": "both",
       "departure_alerts_enabled": True}


@pytest.fixture
def m(load, monkeypatch):
    mig = load("migrations")
    nc = load("nova_config")
    saved = {}
    monkeypatch.setattr(nc, "get", lambda k, d=None: saved.get(k, d))
    mig.saved = saved
    return mig


def test_a_fresh_install_saves_none_of_them(m):
    _, options, _ = m.migrate_config({}, {}, current_version=m.CURRENT_SCHEMA_VERSION)
    assert not set(OLD) & set(options)


def test_an_existing_install_keeps_the_old_defaults(m):
    _, options, ver = m.migrate_config({}, {}, current_version=8)
    assert ver == 9
    assert {k: options[k] for k in OLD} == OLD


@pytest.mark.parametrize("where", ["options", "data", "config_json"])
def test_a_saved_value_is_never_overwritten(m, where):
    data, options = {}, {}
    if where == "options":
        options["package_detection"] = False
    elif where == "data":
        data["package_detection"] = False
    else:
        m.saved["package_detection"] = False
    _, out, _ = m.migrate_config(data, options, current_version=8)
    assert out.get("package_detection", "absent") == (False if where == "options" else "absent")
    assert out["recognition_source"] == "both"              # the others still kept


def test_package_watch_is_off_by_default(load):
    pm = load("package_monitor")
    from conftest import FakeHass
    assert pm._package_detection_on(FakeHass()) is False


def test_cognition_reads_departure_reminders_off_by_default():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "custom_components/nova"
    cog = (root / "cognition.py").read_text(encoding="utf-8")
    assert 'nova_config.get("departure_alerts_enabled", False)' in cog
