"""LockdownManager.engage(): what it secures, what it leaves, what it says.

* Locks that read unlocked are locked.
* Closeable openings (a garage door cover) are closed.
* A bare window contact cannot be closed: it is named once and left alone.
* A silent engage (adopting a lockdown at start up) still secures the house
  but says nothing, so a restart never re-announces.
* Each command is checked later in the background; engage() itself never
  waits for that check.
"""
import json
import types

import pytest
import sys
from cognitive_safety_kit import _isolated_core, cc, clock, service_calls  # noqa: F401

FRONT = "lock.front"
GARAGE = "cover.garage_door"
WINDOW = "binary_sensor.bedroom_window_1"


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture
def lockdown_mod(load):
    return load("core_lockdown")


@pytest.fixture
def state_file(tmp_path, monkeypatch, cc):
    path = tmp_path / "lockdown.json"
    monkeypatch.setattr(cc, "LOCKDOWN_STATE_PATH", str(path))
    return path


@pytest.fixture
def manager(cc, fake_hass, state_file):
    return cc.LockdownManager(fake_hass, {"honorific": "sir"})


def sent(hass, domain, service):
    """Entity ids of the service calls made to domain.service, in order."""
    return [data.get("entity_id") for dom, svc, data in hass.service_calls
            if (dom, svc) == (domain, service)]


def add_lock(hass, entity_id, state, name=None):
    extra = {"friendly_name": name} if name else {}
    hass.states.set(entity_id, state, **extra)


async def engage_now(manager, hass, *args, **kwargs):
    action = await manager.engage(*args, **kwargs)
    hass.close_pending()   # the background checks are not under test here
    return action


# ── what gets secured ───────────────────────────────────────────────────────

async def test_locks_close_the_garage_and_name_the_window(manager, fake_hass):
    add_lock(fake_hass, FRONT, "unlocked", "Front Lock")
    fake_hass.states.set(GARAGE, "open", device_class="garage", friendly_name="Garage Door")
    fake_hass.states.set(WINDOW, "on", device_class="window",
                         friendly_name="Bedroom Window 1")

    action = await engage_now(manager, fake_hass, "test")

    assert sent(fake_hass, "lock", "lock") == [FRONT]
    assert sent(fake_hass, "cover", "close_cover") == [GARAGE]
    # The window cannot be shut from here, so it is left and named once.
    assert manager.exempt_windows == {WINDOW}
    assert action["type"] == "lockdown_engaged"
    assert action["urgency"] == "high"
    assert "Bedroom Window 1 is open" in action["message"]
    assert "close Garage Door" in action["message"]
    assert action["message"].endswith(
        "lock Front Lock and close Garage Door, but Bedroom Window 1 is open and I "
        "can't secure it remotely — you'll want to close it. I will alert you if "
        "anything does not secure.")


async def test_a_bare_window_contact_is_never_closed(manager, fake_hass):
    fake_hass.states.set("binary_sensor.window", "on", device_class="window")
    await engage_now(manager, fake_hass, "test")
    assert sent(fake_hass, "cover", "close_cover") == []
    assert "binary_sensor.window" in manager.exempt_windows


async def test_a_garage_nova_closed_is_secured_not_exempt(manager, fake_hass):
    fake_hass.states.set(GARAGE, "open", device_class="garage")
    await engage_now(manager, fake_hass, "test")
    assert GARAGE not in manager.exempt_windows
    assert GARAGE in manager._secured_by_us


async def test_a_lock_already_locked_gets_no_command(manager, fake_hass):
    add_lock(fake_hass, FRONT, "locked")
    add_lock(fake_hass, "lock.back", "unlocked")
    await engage_now(manager, fake_hass, "test")
    assert sent(fake_hass, "lock", "lock") == ["lock.back"]


async def test_nothing_to_do_says_the_home_was_already_secure(manager, fake_hass):
    add_lock(fake_hass, FRONT, "locked")
    action = await engage_now(manager, fake_hass, "test")
    assert action["message"].endswith("the home was already fully secured.")
    assert fake_hass.service_calls == []


# ── engaging once, saving, staying quiet ────────────────────────────────────

async def test_a_second_engage_does_nothing(manager, fake_hass):
    add_lock(fake_hass, FRONT, "unlocked")
    assert await engage_now(manager, fake_hass, "first") is not None
    calls_after_first = list(fake_hass.service_calls)
    assert await engage_now(manager, fake_hass, "second") is None
    assert fake_hass.service_calls == calls_after_first
    assert manager.reason == "first"


async def test_the_engaged_state_is_saved_for_a_restart(manager, fake_hass, state_file):
    fake_hass.states.set(WINDOW, "on", device_class="window")
    await engage_now(manager, fake_hass, "test")
    saved = json.loads(state_file.read_text())
    assert saved["active"] is True
    assert saved["reason"] == "test"
    assert saved["auto"] is False
    assert saved["exempt_windows"] == [WINDOW]


async def test_a_silent_engage_secures_but_says_nothing(manager, fake_hass, state_file):
    add_lock(fake_hass, FRONT, "unlocked")
    manager.set_automatic_lockdown(True)
    action = await engage_now(manager, fake_hass, "startup", auto=True, announce=False)
    assert action is None                 # no announcement on adoption
    assert manager.active is True         # but the lockdown is on
    assert manager.auto is True
    assert sent(fake_hass, "lock", "lock") == [FRONT]
    assert json.loads(state_file.read_text())["active"] is True


async def test_an_automatic_engage_with_the_setting_off_does_nothing(manager, fake_hass):
    add_lock(fake_hass, FRONT, "unlocked")
    action = await engage_now(manager, fake_hass, "alarm armed", auto=True)
    assert action is None
    assert manager.active is False
    assert fake_hass.service_calls == []


# ── background checks ───────────────────────────────────────────────────────

async def test_lock_all_reports_id_and_name_pairs(manager, fake_hass):
    add_lock(fake_hass, FRONT, "unlocked", "Front Lock")
    pairs = await manager._lock_all()
    fake_hass.close_pending()
    assert pairs == [(FRONT, "Front Lock")]


async def test_every_lock_sent_a_command_is_checked_later(manager, fake_hass, monkeypatch):
    add_lock(fake_hass, FRONT, "unlocked", "Front Lock")
    add_lock(fake_hass, "lock.back", "unlocked", "Back Lock")
    add_lock(fake_hass, "lock.side", "locked", "Side Lock")   # nothing to do
    checked = []

    async def note_check(eid, dom, name):
        checked.append((eid, dom, name))

    monkeypatch.setattr(manager, "_verify_secured", note_check)
    action = await manager.engage("test")
    await fake_hass.drain()

    assert sorted(checked) == [("lock.back", "lock", "Back Lock"),
                               (FRONT, "lock", "Front Lock")]
    # The wording still names the locks a command went to.
    assert "Front Lock" in action["message"] and "Back Lock" in action["message"]


async def test_engage_returns_before_any_check_waits(manager, fake_hass, lockdown_mod,
                                                     monkeypatch):
    """The check waits LOCKDOWN_SECURE_VERIFY_DELAY; engage() must schedule it,
    never await it."""
    add_lock(fake_hass, FRONT, "unlocked", "Front Lock")

    async def must_not_wait(_seconds):
        raise AssertionError("engage() awaited the verification delay")

    monkeypatch.setattr(lockdown_mod, "asyncio", types.SimpleNamespace(sleep=must_not_wait))
    action = await manager.engage("test")
    assert action is not None
    fake_hass.close_pending()


@pytest.fixture
def no_delay_and_alerts(cc, lockdown_mod, monkeypatch):
    """Run the background check at once and collect what it would announce."""
    async def at_once(_seconds):
        return None

    monkeypatch.setattr(lockdown_mod, "asyncio", types.SimpleNamespace(sleep=at_once))
    raised = []

    async def collect(hass, config, action, sleeping):
        raised.append(action)

    monkeypatch.setattr(cc, "_emit_action", collect)
    return raised


async def test_a_lock_that_never_locks_raises_a_critical_alert(manager, fake_hass,
                                                               no_delay_and_alerts):
    add_lock(fake_hass, FRONT, "unlocked", "Front Lock")   # the fake never changes it
    await manager.engage("test")
    await fake_hass.drain()
    assert len(no_delay_and_alerts) == 1
    alert = no_delay_and_alerts[0]
    assert alert["type"] == "lockdown_breach"
    assert alert["urgency"] == "critical"
    assert "tried to secure Front Lock" in alert["message"]
    assert "still unlocked" in alert["message"]


async def test_a_lock_that_did_lock_raises_nothing(manager, fake_hass, no_delay_and_alerts):
    add_lock(fake_hass, FRONT, "unlocked", "Front Lock")
    await manager.engage("test")
    add_lock(fake_hass, FRONT, "locked", "Front Lock")       # the command took
    await fake_hass.drain()
    assert no_delay_and_alerts == []


async def test_a_garage_that_stays_open_is_reported_open(manager, fake_hass,
                                                         no_delay_and_alerts):
    fake_hass.states.set(GARAGE, "open", device_class="garage", friendly_name="Garage Door")
    await manager.engage("test")
    await fake_hass.drain()
    assert [a["type"] for a in no_delay_and_alerts] == ["lockdown_breach"]
    assert "Garage Door" in no_delay_and_alerts[0]["message"]
    assert "still open" in no_delay_and_alerts[0]["message"]


async def test_no_check_alerts_once_the_lockdown_is_lifted(manager, fake_hass,
                                                           no_delay_and_alerts):
    add_lock(fake_hass, FRONT, "unlocked", "Front Lock")
    await manager.engage("test")
    await manager.disengage("done")
    await fake_hass.drain()
    assert no_delay_and_alerts == []


# ── the lockdown message's honorific follows who is home ──────────────────────

def _set_nova_config(mod, monkeypatch, mapping):
    """Same convention as test_honorific.py / test_announce_resilience.py:
    monkeypatch the lazily-imported `nova_config` module in the same
    synthetic package as `mod`, since every consumer does `from . import
    nova_config` inside a function body, not at module level."""
    pkg = mod.__name__.rsplit(".", 1)[0]
    fake = types.SimpleNamespace(get=lambda k, d=None: mapping.get(k, d))
    monkeypatch.setitem(sys.modules, f"{pkg}.nova_config", fake)


@pytest.fixture
def lockdown(cognitive_core, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(cognitive_core, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))
    return cognitive_core.LockdownManager(fake_hass, {"honorific": "sir"})


async def test_lockdown_engage_uses_solo_persons_own_honorific(
    cognitive_core, lockdown, fake_hass, monkeypatch
):
    fake_hass.states.set("person.morgan", "home", friendly_name="Morgan")
    fake_hass.states.set("lock.front", "locked")
    _set_nova_config(cognitive_core, monkeypatch, {
        "person_honorifics": {"person.morgan": "boss"},
        "honorific": "sir",
    })
    action = await lockdown.engage("test")
    fake_hass.close_pending()
    assert action["message"].startswith("Boss,")


async def test_lockdown_engage_drops_honorific_when_multiple_home(
    cognitive_core, lockdown, fake_hass, monkeypatch
):
    fake_hass.states.set("person.alex", "home", friendly_name="Alex")
    fake_hass.states.set("person.morgan", "home", friendly_name="Morgan")
    fake_hass.states.set("lock.front", "locked")
    _set_nova_config(cognitive_core, monkeypatch, {"honorific": "sir"})
    action = await lockdown.engage("test")
    fake_hass.close_pending()
    # Still a complete, correctly-capitalized sentence — just no address.
    assert action["message"][0].isupper()
    assert not action["message"].startswith(("Sir,", "Boss,"))


# ── lockdown records each check against the lockdown ──────────────────────────

@pytest.fixture
def records(load, tmp_path, monkeypatch):
    dr = load("decision_record")
    monkeypatch.setattr(dr, "_DEFAULT_DB", str(tmp_path / "decisions.db"))
    return lambda kind=None: dr.recent(limit=50, kind=kind)


def obey(fake_hass, *jammed):
    """Devices change state when commanded, except the jammed ones."""
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        new = {("lock", "lock"): "locked", ("cover", "close_cover"): "closed"}.get(
            (domain, service))
        if isinstance(eid, list):
            eids = eid
        else:
            eids = [eid]
        for e in eids:
            st = fake_hass.states.get(e)
            if st is not None and new and e not in jammed:
                fake_hass.states.set(e, new, **dict(st.attributes))
    fake_hass.services.async_call = call


async def test_lockdown_records_its_checks_linked_to_the_lockdown(load, cc, fake_hass,
                                                                  records, monkeypatch):
    monkeypatch.setattr(sys.modules["jc.core_lockdown"], "LOCKDOWN_SECURE_VERIFY_DELAY", 0)
    obey(fake_hass)
    mgr = cc.LockdownManager(fake_hass, {})
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("lock.side", "unlocked", friendly_name="Side")
    await mgr.engage("requested")
    fake_hass.states.remove("lock.side")         # cannot be read at the check
    await fake_hass.drain()
    rows = records("lockdown")
    by = {r["decision"]: r for r in rows}
    assert {"lockdown engaged", "checked, secured", "could not check"} <= set(by)
    refs = {r["ref"] for r in rows}
    assert len(refs) == 1 and next(iter(refs)).startswith("lockdown:")
    assert by["could not check"]["interpretation"] == {"assessment": "unverified"}


# ── arming does not name a fridge door ────────────────────────────────────────

async def test_arming_with_the_fridge_open_does_not_name_the_fridge(cc, fake_hass):
    mgr = cc.LockdownManager(fake_hass, {})
    fake_hass.states.set("binary_sensor.fridge_door", "on", device_class="door",
                         friendly_name="Fridge door")
    fake_hass.states.set("binary_sensor.kitchen_window", "on", device_class="window",
                         friendly_name="Kitchen window")
    action = await mgr.engage("test")
    assert "Kitchen window" in action["message"]
    assert "Fridge" not in action["message"]
    assert "binary_sensor.fridge_door" not in mgr.exempt_windows
