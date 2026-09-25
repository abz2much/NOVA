"""Regression tests for the Action Audit Log's ownership rule: the
top-level component that understands the user's/system's intended action
creates the request_id and row(s); a lower-level helper it calls never
creates a second, duplicate record for the same intent — it either reuses
the caller's request_id or does nothing to the log at all.

Each test proves ONE request_id and the expected row COUNT for a scenario
the design report calls out explicitly as needing duplicate prevention.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture
def isolated_db(al, monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    monkeypatch.setattr(al, "_DEFAULT_DB", path)
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass


class _Services:
    def __init__(self):
        self.calls = []
        self.inventory = None  # set when a test models HA's loaded automations

    async def async_call(self, domain, service, data=None, blocking=False, target=None, **kw):
        self.calls.append((domain, service, dict(data or {})))
        if self.inventory is not None and (domain, service) == ("automation", "reload"):
            self.inventory.reload()


def _loaded_automations(load, monkeypatch, hass, path):
    """Give hass a loaded-automation list that follows reloads; automation
    installation fails closed without one (Phase 5)."""
    from fakes import FakeAutomationInventory
    hass.services.inventory = FakeAutomationInventory(path)
    monkeypatch.setattr(load("automation.inventory"), "get_inventory",
                        lambda _hass: hass.services.inventory)


class _States:
    def __init__(self, states=None):
        self._states = states or {}

    def async_all(self, domain):
        return list(self._states.get(domain, []))

    def get(self, eid):
        return self._states.get(eid)


class _FakeHass:
    def __init__(self, states=None):
        self.services = _Services()
        self.states = _States(states)
        self.config = type("Cfg", (), {"path": lambda self, *p: os.path.join(
            tempfile.gettempdir(), *p)})()

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro, name=None):
        coro.close()


# ── Suggestion installation calling automation creation ─────────────────────

async def test_suggestion_install_and_automation_create_share_one_request(
        load, isolated_db, al, monkeypatch, tmp_path):
    pattern_analyzer = load("pattern_analyzer")
    automation_creator = load("automation_creator")

    class _Analyzer:
        async def get_suggestion_async(self, *a, **k): ...
    def get_suggestion(sid):
        return {"automation_yaml": "irrelevant", "description": "desc"}
    def approve_suggestion(sid):
        return True
    def mark_installed(sid, aid):
        return True

    fake_analyzer = type("A", (), {
        "get_suggestion": staticmethod(get_suggestion),
        "approve_suggestion": staticmethod(approve_suggestion),
        "mark_installed": staticmethod(mark_installed),
    })()
    monkeypatch.setattr(pattern_analyzer, "get_analyzer", lambda: fake_analyzer)
    monkeypatch.setattr(pattern_analyzer, "normalize_suggestion_automation", lambda yaml: {
        "installable": True, "alias": "Test Automation",
        "trigger": {"platform": "state"}, "action": {"service": "light.turn_on"},
    })

    automations_file = tmp_path / "automations.yaml"

    class _Hass(_FakeHass):
        def __init__(self):
            super().__init__()
            self.config = type("Cfg", (), {
                "path": lambda self, *p: str(automations_file) if p == ("automations.yaml",)
                else os.path.join(str(tmp_path), *p),
            })()

    hass = _Hass()
    _loaded_automations(load, monkeypatch, hass, automations_file)
    result = await pattern_analyzer.install_approved_suggestion(hass, 42)
    assert result["ok"] is True
    assert result["installed"] is True

    # automation.reload must have fired (supporting work)...
    assert ("automation", "reload") in [(d, s) for d, s, _ in hass.services.calls]

    # ...but exactly ONE action_log row exists for this whole flow — the
    # reload does not get its own row, and install_approved_suggestion does
    # not create a second row alongside create_automation's.
    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    row = page["requests"][0]
    assert len(row["targets"]) == 1
    assert row["action"] == "create_automation"
    assert row["source"] == "suggestion"
    assert row["targets"][0]["execution_result"] == "accepted"


async def test_create_automation_as_direct_top_level_call_gets_its_own_request(
        load, isolated_db, al, tmp_path, monkeypatch):
    """The SAME function, called directly (not via a suggestion), is a
    genuine top-level action and mints its own request_id — this is not a
    duplicate-prevention violation, it's the correct behavior for an
    independently-invoked call."""
    automation_creator = load("automation_creator")
    automations_file = tmp_path / "automations.yaml"

    class _Hass(_FakeHass):
        def __init__(self):
            super().__init__()
            self.config = type("Cfg", (), {"path": lambda self, *p: str(automations_file)})()

    hass = _Hass()
    _loaded_automations(load, monkeypatch, hass, automations_file)
    result = await automation_creator.create_automation(
        hass, alias="Direct Test", trigger={"platform": "state"},
        action={"service": "light.turn_on"}, source="ha_service",
    )
    assert result["success"] is True
    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    assert page["requests"][0]["source"] == "ha_service"


# ── Mode changes calling individual light services ───────────────────────

async def test_mode_change_light_dimming_is_one_request(load, isolated_db, al, monkeypatch):
    mode_scene = load("mode_scene")

    nova_config_fake = type("M", (), {
        "get": staticmethod(lambda key, default=None: {
            "movie_area": "living_room", "movie_dim_pct": 15,
        }.get(key, default)),
    })()
    audio_routing_fake = type("M", (), {
        "entity_area": staticmethod(lambda hass, eid: "living_room"),
    })()
    import sys, types
    stub_nc = types.ModuleType("jc.nova_config")
    stub_nc.get = nova_config_fake.get
    monkeypatch.setitem(sys.modules, "jc.nova_config", stub_nc)
    stub_ar = types.ModuleType("jc.audio_routing")
    stub_ar.entity_area = audio_routing_fake.entity_area
    monkeypatch.setitem(sys.modules, "jc.audio_routing", stub_ar)

    light_state = type("S", (), {"entity_id": "light.living_room_lamp"})()
    hass = _FakeHass(states={"light": [light_state]})

    await mode_scene.apply_mode_entry(hass, "movie", source="voice")

    assert ("light", "turn_on") in [(d, s) for d, s, _ in hass.services.calls]
    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    assert page["requests"][0]["action"] == "mode_entry_mood"
    assert len(page["requests"][0]["targets"]) == 1  # one call, one row -- not one per light


# ── Verification retries update the existing row, never create a new one ────

async def test_verify_control_retry_updates_existing_row_not_a_new_one(
        load, isolated_db, al, monkeypatch):
    agent = load("agent")

    rid = al.new_request_id()
    aid = al.start(rid, "control_device", "voice", domain="lock",
                    entity_id="lock.front_door", db_path=isolated_db)
    al.set_execution(aid, "accepted", db_path=isolated_db)

    # Force the "not confirmed on first check, retry, then succeed" path.
    calls = {"n": 0}
    def fake_state_ok(hass, entity_id, expected):
        calls["n"] += 1
        return False if calls["n"] == 1 else True

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(agent, "_state_ok", fake_state_ok)
    monkeypatch.setattr(agent, "_VERIFY_SLEEP", _no_sleep)

    hass = _FakeHass()
    await agent._verify_control(
        hass, "lock.front_door", "lock", "lock", "lock",
        {"entity_id": "lock.front_door"}, action_id=aid,
    )

    # Still exactly one request/row for this action -- the retry updated it,
    # it did not create a second logged action.
    page = al.page_requests(limit=10, db_path=isolated_db)
    assert len(page["requests"]) == 1
    assert page["requests"][0]["targets"][0]["execution_result"] == "verified"
    assert page["requests"][0]["targets"][0]["reason_code"] == "verified_on_retry"


# ── Logging failure must never alter the real action ─────────────────────

async def test_action_log_total_failure_does_not_alter_the_real_action(load, monkeypatch):
    """The database backing action_log is entirely unwritable (the realistic
    failure mode — a locked or read-only file) for the whole duration of
    the call. The device must still turn on, verification must still run,
    and the reported status must still be correct — logging is fail-open,
    never load-bearing for the action itself. Every action_log.* function
    already wraps its own body in try/except internally (proven by
    test_action_log.py's own unwritable-database tests); this test proves
    that guarantee holds through the real _exec_control_device call path,
    not just when action_log is called directly."""
    agent = load("agent")
    al = load("action_log")

    def _connect_boom(db_path):
        raise OSError("Read-only file system")

    monkeypatch.setattr(al, "_connect", _connect_boom)

    class _States:
        def __init__(self):
            self._s = {"light.den": type("S", (), {
                "state": "off", "attributes": {"friendly_name": "Den Light"}})()}
        def get(self, eid):
            return self._s.get(eid)
        def async_all(self, domain=None):
            return list(self._s.values())

    class _Services:
        def __init__(self, states):
            self._states = states
        async def async_call(self, domain, service, data=None, blocking=False, **kw):
            if domain == "light" and service == "turn_on":
                self._states._s["light.den"] = type("S", (), {
                    "state": "on", "attributes": {"friendly_name": "Den Light"}})()

    class _Hass:
        def __init__(self):
            self.states = _States()
            self.services = _Services(self.states)
        async def async_add_executor_job(self, func, *args):
            return func(*args)
        def async_create_task(self, coro, name=None):
            coro.close()

    hass = _Hass()
    out = await agent._exec_control_device(hass, {"entity_id": "light.den", "action": "turn_on"})

    assert '"success": true' in out.lower()
    assert hass.states.get("light.den").state == "on"  # the real action happened
