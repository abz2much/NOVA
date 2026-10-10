"""Borrowed and personal leftovers removed (8.28.0).

* 2a: the infrastructure audit watches only the sensors the user lists
  (infrastructure_audit_sensors); see also test_infrastructure_triage.py.
* 2c: the word fix that turned "chaise" into "chase" is gone.
* 2d: nothing is exempt from lockdown by default. An install from before
  8.28.0 that never saved the setting keeps its two thermostat locks exempt,
  written into its saved setting once by the v7 to v8 migration.
* 2e: the hazard monitor's user agent says "Nova".
All state is fake; nothing touches a real lock.
"""
import pathlib
import subprocess

import pytest

from cognitive_safety_kit import _isolated_core, cc  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[2]
OLD = ["lock.downstairs_thermo_lock", "lock.upstairs_thermo_lock"]


@pytest.fixture
def m(load, monkeypatch):
    mig = load("migrations")
    nc = load("nova_config")
    saved = {}
    monkeypatch.setattr(nc, "get", lambda k, d=None: saved.get(k, d))
    mig.saved = saved
    return mig


def _obey(fake_hass):
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        st = fake_hass.states.get(eid) if isinstance(eid, str) else None
        if st is not None and (domain, service) == ("lock", "lock"):
            fake_hass.states.set(eid, "locked", **dict(st.attributes))
    fake_hass.services.async_call = call


def _locked(fake_hass):
    return sorted(c[2]["entity_id"] for c in fake_hass.service_calls if c[:2] == ("lock", "lock"))


# ── 2d: a safety default change, with a migration ───────────────────────────

def test_the_built_in_default_is_empty(load):
    assert load("core_common").LOCKDOWN_EXEMPT_LOCKS_DEFAULT == set()


async def test_a_fresh_install_locks_every_lock(cc, fake_hass, m):
    # A fresh entry starts at the current schema: no migration, nothing saved.
    _, options, ver = m.migrate_config({}, {}, current_version=m.CURRENT_SCHEMA_VERSION)
    assert "lockdown_exempt_locks" not in options
    _obey(fake_hass)
    for eid in OLD + ["lock.front"]:
        fake_hass.states.set(eid, "unlocked")
    await cc.LockdownManager(fake_hass, dict(options)).engage("alarm armed")
    fake_hass.close_pending()
    assert _locked(fake_hass) == sorted(OLD + ["lock.front"])


async def test_a_migrated_install_keeps_its_two_locks_exempt(cc, fake_hass, m):
    _, options, ver = m.migrate_config({}, {}, current_version=7)
    assert ver == 8 and options["lockdown_exempt_locks"] == OLD
    _obey(fake_hass)
    for eid in OLD + ["lock.front"]:
        fake_hass.states.set(eid, "unlocked")
    await cc.LockdownManager(fake_hass, dict(options)).engage("alarm armed")
    fake_hass.close_pending()
    assert _locked(fake_hass) == ["lock.front"]


def test_the_migration_runs_once(m):
    _, options, ver = m.migrate_config({}, {}, current_version=7)
    options["lockdown_exempt_locks"] = ["lock.mine"]           # the user changes it later
    _, again, _ = m.migrate_config({}, dict(options), current_version=ver)
    assert again["lockdown_exempt_locks"] == ["lock.mine"]


@pytest.mark.parametrize("where", ["options", "data", "config_json"])
@pytest.mark.parametrize("value", [[], ["lock.mine"]])
def test_a_saved_value_is_never_overwritten(m, where, value):
    data, options = {}, {}
    if where == "options":
        options["lockdown_exempt_locks"] = value
    elif where == "data":
        data["lockdown_exempt_locks"] = value
    else:
        m.saved["lockdown_exempt_locks"] = value
    _, out, ver = m.migrate_config(data, options, current_version=7)
    assert ver == 8
    assert out.get("lockdown_exempt_locks", "absent") == (value if where == "options" else "absent")


def test_fresh_and_imported_entries_start_at_the_right_schema():
    flow = (ROOT / "custom_components/nova/config_flow.py").read_text(encoding="utf-8")
    assert '"schema_version": CURRENT_SCHEMA_VERSION,' in flow   # the setup dialog: fresh
    assert '"schema_version": 7,' in flow                         # import of an old config.json


# ── 2c: the "chaise" word fix ───────────────────────────────────────────────

def test_the_chaise_word_fix_is_gone(load):
    corrections = load("local_engine")._STT_CORRECTIONS
    assert "chaise" not in corrections and "chase" not in corrections
    assert corrections["lamp"] == "light"                       # the rest is unchanged


# ── 2a: the audit's sensor list ─────────────────────────────────────────────

def _audit_sensors_fn(load):
    """proactive_audio cannot be imported whole here, so its _audit_sensors is
    compiled on its own, with the real nova_config next to it."""
    import ast
    load("nova_config")
    src = (ROOT / "custom_components/nova/proactive_audio.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "_audit_sensors")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "AUDIT_SENSORS_KEY"
                                                    for t in n.targets))]
    ns = {"__name__": "jc._audit", "__package__": "jc"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "proactive_audio.py", "exec"), ns)
    return ns["_audit_sensors"]


@pytest.mark.parametrize("raw,want", [
    (None, []), ([], []), ("", []), ("[]", []), ("{not json", []), (5, []),
    (["sensor.disk_use", "", 3, "binary_sensor.switch"], ["sensor.disk_use", "binary_sensor.switch"]),
    ('["sensor.disk_use"]', ["sensor.disk_use"]),
])
def test_the_audit_reads_its_sensor_list(load, monkeypatch, raw, want):
    fn = _audit_sensors_fn(load)
    nc = load("nova_config")
    seen = []

    def _get(h, e, k, d=None):
        seen.append(k)
        return raw if raw is not None else d
    monkeypatch.setattr(nc, "runtime_get", _get)
    assert fn(None, None) == want
    assert seen == ["infrastructure_audit_sensors"]


def test_the_audit_is_given_the_list(load):
    src = (ROOT / "custom_components/nova/proactive_audio.py").read_text(encoding="utf-8")
    assert "sensors=_audit_sensors(hass, entry)" in src


# ── 2e: the user agent ──────────────────────────────────────────────────────

def test_the_hazard_user_agent_says_nova(load):
    ua = load("hazard_monitor")._USER_AGENT
    assert ua == "Nova Home Assistant hazard monitor (github.com/abz2much/nova)"


# ── no borrowed examples or names left where people see them ───────────────

def test_the_old_examples_are_gone_outside_tests():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    shown = [f for f in files if not f.startswith(("tests/", "scripts/smoke_panel.js", "scripts/late_text_check.js"))
             and f != "CHANGELOG.md" and f.endswith((".py", ".js", ".json", ".md", ".yaml", ".svg"))]
    old = ("sam owns", "Sam owns", "car.jeep", "the Jeep", "Biscuit", "chase lamp", "'chase'",
           "camera.workshop", "Nova-AIO", "nova-aio", "AIO principle", "home_cove", "kitchen_dog",
           "thermo_lock", "core_switch", "server_root_storage", "basement_freeze_sensor",
           "chaise", "is next to the garage")
    hits = []
    for f in shown:
        text = (ROOT / f).read_text(encoding="utf-8", errors="ignore")
        hits += [(f, w) for w in old if w in text]
    allowed = {("custom_components/nova/core_common.py", "thermo_lock")}   # the migration's old list
    assert [h for h in hits if h not in allowed] == []


def test_the_installed_copy_carries_the_licence():
    assert (ROOT / "custom_components/nova/LICENSE").read_text() == (ROOT / "LICENSE").read_text()
