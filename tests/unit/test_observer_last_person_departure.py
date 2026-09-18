"""End-to-end: a genuine last-person departure must not speak into the
house, and must send the configured phone notification instead.

Reproduces the 2026-09-18 08:30 incident: person.abi went home -> not_home
while a phone-presence binary_sensor (device_class="presence") was still
"on" for ~63s after the person entity had already gone not_home (a
clear-delay). audio_routing.anyone_home(hass) — called independently at
routing time, before the fix — counted that stale sensor and returned True,
so the MEDIUM branch's "if not home: notify_only" guard never triggered and
Nova spoke "Abi has left the premises." into an empty house with no phone
backup at all.

This test drives observer.py's real _process_event() pipeline (the actual
production call path) with a FakeHass exposing the stale sensor, and
asserts: no tts.speak call happens, and the configured notify service is
called instead. Cross-module collaborators that aren't the subject of this
fix (classifier, reasoning_loop's cloud/cache path, output_gate, sleep
detection) are stubbed directly, matching the rest of this test suite's
hand-rolled-fake convention; audio_routing itself is the REAL fixed module,
so this proves the authoritative_anyone_home wiring, not a mock of it.
"""
from types import SimpleNamespace


class _States:
    def __init__(self, presence_sensor_on: bool):
        self._presence_sensor_on = presence_sensor_on

    def async_all(self, domain=None):
        if domain == "person":
            return [SimpleNamespace(state="not_home", attributes={})]
        if domain == "device_tracker":
            return [SimpleNamespace(state="not_home", attributes={})]
        if domain == "binary_sensor" and self._presence_sensor_on:
            # The stale phone-presence sensor from the real incident: still
            # "on" after the person entity already went not_home.
            return [SimpleNamespace(
                state="on",
                attributes={"device_class": "presence"},
                entity_id="binary_sensor.abi_s26_ultra_presence",
            )]
        return []

    def get(self, entity_id):
        return None


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, target=None, blocking=False):
        self.calls.append((domain, service, dict(data)))


class _FakeHass:
    def __init__(self, presence_sensor_on: bool):
        self.states = _States(presence_sensor_on)
        self.services = _Services()
        self.data = {}

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        raise NotImplementedError("not used by _process_event directly")


def _departure_event(observer):
    mk = lambda s: SimpleNamespace(state=s, attributes={"friendly_name": "Abi"})
    return SimpleNamespace(data={
        "entity_id": "person.abi",
        "old_state": mk("home"),
        "new_state": mk("not_home"),
    })


def _wire_fakes(observer, monkeypatch):
    async def fake_classify(*a, **kw):
        return {"worth_considering": True, "urgency": "medium", "category": "presence"}

    async def fake_decide(*a, **kw):
        # The fixed reasoning_loop: a real last-person departure, from_state
        # "home" to_state "not_home", anyone_home False -> medium urgency,
        # correct "left the premises" message.
        assert kw.get("anyone_home") is False
        return {"speak": True, "message": "Abi has left the premises.", "urgency": "medium"}

    monkeypatch.setattr(observer.classifier, "classify", fake_classify)
    monkeypatch.setattr(observer.reasoning_loop, "decide", fake_decide)
    monkeypatch.setattr(observer.audio_routing, "entity_area", lambda h, e: None)
    monkeypatch.setattr(observer.audio_routing, "currently_occupied_areas", lambda h: [])
    monkeypatch.setattr(observer.sleep_detection, "is_sleeping", lambda *a, **k: (False, ""))
    monkeypatch.setattr(observer.sleep_detection, "_in_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(observer.output_gate, "can_announce", lambda **kw: (True, ""))
    monkeypatch.setattr(observer.output_gate, "record_announcement", lambda **kw: None)
    monkeypatch.setattr(observer.output_gate, "recent_announcements", lambda n=5: [])


def test_last_person_departure_notifies_and_never_speaks(load, monkeypatch):
    """Reproduces the incident exactly: stale presence sensor still 'on'.
    Must still route notify_only and call the configured notify service."""
    observer = load("observer")
    _wire_fakes(observer, monkeypatch)
    hass = _FakeHass(presence_sensor_on=True)
    observer._STATE.hass = hass
    observer._STATE.config = {"notify_service": "notify.mobile_app_abi_s26"}
    observer._STATE.classifier_provider = None
    observer._STATE.reasoning_provider = None

    import asyncio
    asyncio.run(
        observer._process_event(_departure_event(observer))
    )

    assert ("tts", "speak") not in {(d, s) for d, s, _ in hass.services.calls}
    notify_calls = [c for c in hass.services.calls if c[0] == "notify"]
    assert len(notify_calls) == 1
    domain, service, data = notify_calls[0]
    assert (domain, service) == ("notify", "mobile_app_abi_s26")
    assert data["message"] == "Abi has left the premises."


def test_last_person_departure_no_stale_sensor_also_notifies(load, monkeypatch):
    """Same scenario without a lingering sensor at all — the non-stale case
    must behave identically (notify_only, no speech)."""
    observer = load("observer")
    _wire_fakes(observer, monkeypatch)
    hass = _FakeHass(presence_sensor_on=False)
    observer._STATE.hass = hass
    observer._STATE.config = {"notify_service": "notify.mobile_app_abi_s26"}
    observer._STATE.classifier_provider = None
    observer._STATE.reasoning_provider = None

    import asyncio
    asyncio.run(
        observer._process_event(_departure_event(observer))
    )

    assert ("tts", "speak") not in {(d, s) for d, s, _ in hass.services.calls}
    notify_calls = [c for c in hass.services.calls if c[0] == "notify"]
    assert len(notify_calls) == 1
