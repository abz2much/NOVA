"""Tests for lockdown engage behaviour (v6.31.0):

  • motorized/closeable openings (garage doors) are CLOSED on engage;
  • bare contacts (windows) can't be closed — alerted once, then left;
  • locks are locked;
  • startup adoption is silent (no announcement) so reboots don't re-notify.
"""
import pytest


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, cc):
    monkeypatch.setattr(cc, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))
    yield


def _mgr(cc, fake_hass):
    return cc.LockdownManager(fake_hass, {"honorific": "sir"})


def _calls(fake_hass, domain, service):
    return [c for c in fake_hass.service_calls if c[0] == domain and c[1] == service]


async def test_engage_locks_and_closes_garage(cc, fake_hass):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")
    fake_hass.states.set("cover.garage_door", "open", device_class="garage",
                         friendly_name="Garage Door")
    fake_hass.states.set("binary_sensor.usernames_window_1", "on", device_class="window",
                         friendly_name="Username's Window 1")
    mgr = _mgr(cc, fake_hass)

    action = await mgr.engage("test")
    fake_hass.close_pending()

    assert _calls(fake_hass, "lock", "lock"), "should lock the unlocked lock"
    closes = _calls(fake_hass, "cover", "close_cover")
    assert closes and closes[0][2].get("entity_id") == "cover.garage_door"

    # the window can't be closed → left open and alerted, never auto-closed
    assert "binary_sensor.usernames_window_1" in mgr.exempt_windows
    assert action and "Username's Window 1 is open" in action["message"]
    assert "close Garage Door" in action["message"]


async def test_bare_window_is_not_closed(cc, fake_hass):
    fake_hass.states.set("binary_sensor.window", "on", device_class="window")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("test")
    fake_hass.close_pending()
    assert not _calls(fake_hass, "cover", "close_cover")
    assert "binary_sensor.window" in mgr.exempt_windows


async def test_closed_garage_not_treated_as_intentional_open(cc, fake_hass):
    fake_hass.states.set("cover.garage_door", "open", device_class="garage")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("test")
    fake_hass.close_pending()
    # we closed it, so it's tracked as secured-by-us, NOT exempted as left-open
    assert "cover.garage_door" not in mgr.exempt_windows
    assert "cover.garage_door" in mgr._secured_by_us


async def test_silent_engage_secures_without_announcing(cc, fake_hass):
    fake_hass.states.set("lock.front", "unlocked")
    mgr = _mgr(cc, fake_hass)
    mgr.set_automatic_lockdown(True)
    action = await mgr.engage("startup", auto=True, announce=False)
    fake_hass.close_pending()
    assert action is None                       # no notification on silent adopt
    assert mgr.active is True                     # but lockdown is active
    assert _calls(fake_hass, "lock", "lock")      # and it still locked up


async def test_nothing_to_do_is_fully_secured_message(cc, fake_hass):
    fake_hass.states.set("lock.front", "locked")   # already locked, nothing open
    mgr = _mgr(cc, fake_hass)
    action = await mgr.engage("test")
    fake_hass.close_pending()
    assert action["message"].endswith("the home was already fully secured.")


# ── Phase 3: the lock step gets the same honest background verification the
# cover/opening step already uses -- reusing _verify_secured() unchanged. ──

async def test_lock_all_returns_entity_id_friendly_name_pairs(cc, fake_hass):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")
    mgr = _mgr(cc, fake_hass)
    locked_pairs = await mgr._lock_all()
    fake_hass.close_pending()
    assert locked_pairs == [("lock.front", "Front Lock")]


async def test_engage_schedules_verify_secured_for_each_locked_entity(cc, fake_hass, monkeypatch):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back Lock")
    fake_hass.states.set("lock.garage_side", "locked", friendly_name="Side Lock")  # already secure
    mgr = _mgr(cc, fake_hass)

    verified = []
    async def fake_verify(eid, dom, name):
        verified.append((eid, dom, name))
    monkeypatch.setattr(mgr, "_verify_secured", fake_verify)

    action = await mgr.engage("test")
    await fake_hass.drain()

    assert sorted(verified) == sorted([
        ("lock.front", "lock", "Front Lock"),
        ("lock.back", "lock", "Back Lock"),
    ])
    # message wording is unaffected -- still names the locks that were locked
    assert "Front Lock" in action["message"] or "locked" in action["message"].lower()


async def test_engage_does_not_block_on_background_verification(cc, fake_hass, monkeypatch):
    """The immediate response must not wait the real 25s LOCKDOWN_SECURE_
    VERIFY_DELAY -- _verify_secured() is scheduled, never awaited inline."""
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")
    mgr = _mgr(cc, fake_hass)

    async def real_sleep_would_hang(_secs):
        raise AssertionError("engage() must not await asyncio.sleep directly")
    monkeypatch.setattr(cc.asyncio, "sleep", real_sleep_would_hang)

    action = await mgr.engage("test")   # must return without hitting the patched sleep
    assert action is not None
    fake_hass.close_pending()


async def test_verify_secured_still_alerts_on_persistent_lock_failure(
    cc, fake_hass, monkeypatch,
):
    """Phase 3 only changed engage()'s IMMEDIATE announcement -- the separate
    background _verify_secured() poll-and-alert mechanism (explicitly out of
    scope, unchanged) must still fire when a lock genuinely never secures."""
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Lock")
    mgr = _mgr(cc, fake_hass)

    async def fast_sleep(_secs):
        pass
    monkeypatch.setattr(cc.asyncio, "sleep", fast_sleep)

    emitted = []
    async def fake_emit(hass, config, action, sleeping):
        emitted.append(action)
    monkeypatch.setattr(cc, "_emit_action", fake_emit)

    await mgr.engage("test")
    await fake_hass.drain()   # let the real (now-fast) _verify_secured() run

    assert len(emitted) == 1
    assert emitted[0]["type"] == "lockdown_breach"
    assert "tried to secure" in emitted[0]["message"]
    assert "still open" in emitted[0]["message"] or "still" in emitted[0]["message"]
