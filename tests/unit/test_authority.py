"""Stage B (8.24.0): one authority check, policy.authorize.

* A scan of the package: every call that can change a lock, cover, alarm,
  scene or script (or any service named at run time) goes through
  policy.authorize / authorize_now, and nothing outside policy.py calls the
  confirmation mechanism (confirm_gate, requires_confirmation) directly.
* The action x source x outcome table, with the real policy and a fake
  confirmation layer.
* Disarm by voice needs a phone tap, with voice confirmation on and off.
* The voice "secure" reply and the offers executor go through it and log.

All state here is fake. Nothing touches a real alarm, lock or cover.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
SECURITY_DOMAINS = {"lock", "cover", "alarm_control_panel", "scene", "script"}
AUTHORITY = {"authorize", "authorize_now"}

# Service calls whose domain is named at run time but is never a lock, cover,
# alarm, scene or script. Each needs a reason.
NOT_SECURITY = {
    "google_travel.py:route_minutes": "Google travel time lookup",
    "notify_targets.py:async_send_configured_notifications": "phone notifications",
    "proactive_audio.py:_run_audit": "Nova's own audit service",
    "voice_confirm.py:_start_listening": "starts a satellite listening for a yes or no",
}


def _modules():
    for f in sorted(ROOT.rglob("*.py")):
        if "__pycache__" not in f.parts:
            yield f, ast.parse(f.read_text(encoding="utf-8"))


def _called_names(fn) -> set:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                out.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                out.add(n.func.attr)
    return out


def _authorizing_functions(tree) -> set:
    """Functions in this module that call authorize, directly or through
    other functions of the same module."""
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    calls = {name: _called_names(fn) for name, fn in fns.items()}
    good = {name for name, c in calls.items() if c & AUTHORITY}
    changed = True
    while changed:
        changed = False
        for name, c in calls.items():
            if name not in good and c & good:
                good.add(name)
                changed = True
    return good


def _service_calls():
    """(module, function, domain or None for dynamic, line) for every
    *.services.async_call that is a security domain or a dynamic one."""
    found = []
    for path, tree in _modules():
        parents = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parents[c] = n
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "async_call"
                    and "services" in ast.unparse(n.func)):
                continue
            first = n.args[0] if n.args else None
            literal = first.value if isinstance(first, ast.Constant) else None
            if literal is not None and literal not in SECURITY_DOMAINS:
                continue
            fn = n
            while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = parents[fn]
            found.append((path, tree, getattr(fn, "name", "<module>"), fn, literal, n.lineno))
    return found


def test_every_security_or_dynamic_service_call_goes_through_authorize():
    missing = []
    for path, tree, name, fn, literal, line in _service_calls():
        rel = path.relative_to(ROOT).as_posix()
        if literal is None and f"{rel}:{name}" in NOT_SECURITY:
            continue
        if name in _authorizing_functions(tree) or (_called_names(fn) & AUTHORITY):
            continue
        missing.append(f"{rel}:{line} {name} ({literal or 'dynamic domain'})")
    assert missing == [], "service calls that skip policy.authorize:\n" + "\n".join(missing)


def test_the_scan_finds_the_known_call_sites():
    # Guards the scan itself: if it stopped finding calls it would pass blindly.
    names = {(p.relative_to(ROOT).as_posix(), n) for p, _t, n, _f, _l, _ln in _service_calls()}
    for expected in (("agent_runtime/capabilities/control.py", "_exec_control_device"),
                     ("core_lockdown.py", "secure_device"),
                     ("intent/intent_router.py", "_call_domain_in_area"),
                     ("core_delivery.py", "_execute_action_data"),
                     ("scenes.py", "async_activate_by_intent")):
        assert expected in names, expected


def test_every_not_security_exception_still_exists():
    present = {f"{p.relative_to(ROOT).as_posix()}:{n}"
               for p, _t, n, _f, lit, _ln in _service_calls() if lit is None}
    assert set(NOT_SECURITY) <= present, set(NOT_SECURITY) - present


def test_nothing_outside_policy_calls_the_confirmation_mechanism():
    offenders = []
    for path, tree in _modules():
        if path.name == "policy.py":
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = (n.func.attr if isinstance(n.func, ast.Attribute)
                        else getattr(n.func, "id", ""))
                if name in ("confirm_gate", "requires_confirmation"):
                    offenders.append(f"{path.relative_to(ROOT)}:{n.lineno}")
    assert offenders == []


# ── the table ───────────────────────────────────────────────────────────────

class _VC:
    """A fake confirmation layer that records what it was asked."""

    def __init__(self):
        self.enabled = False
        self.satellites = set()
        self.spoken_answer = "approved"
        self.phone_answer = "approved"
        self.asked = []

    def install(self, vc, monkeypatch):
        monkeypatch.setattr(vc, "is_enabled", lambda hass: self.enabled)
        monkeypatch.setattr(vc, "is_voice_satellite_device",
                            lambda hass, d, strict=False: d in self.satellites)

        async def spoken(hass, q, entity_id=""):
            self.asked.append(("spoken", q))
            return self.spoken_answer

        async def phone(hass, q):
            self.asked.append(("phone", q))
            return self.phone_answer
        monkeypatch.setattr(vc, "confirm_typed", spoken)
        monkeypatch.setattr(vc, "confirm_via_phone_only_typed", phone)


@pytest.fixture
def pol(load, monkeypatch):
    p = load("policy")
    fake = _VC()
    fake.install(load("voice_confirm"), monkeypatch)
    return p, fake


def _req(p, domain, service, source, entity="", device=""):
    return p.AuthorityRequest(domain, service, entity or f"{domain}.x",
                              source=source, device_id=device)


TABLE = [
    # (domain, service, source, voice confirm on, expected allowed, asked how)
    ("light", "turn_on", "chat", False, True, None),
    ("light", "turn_on", "automatic", False, True, None),
    ("lock", "lock", "automatic", False, True, None),
    ("cover", "close_cover", "automatic", False, True, None),
    ("lock", "unlock", "automatic", False, False, None),
    ("cover", "open_cover", "automatic", False, False, None),
    ("alarm_control_panel", "alarm_disarm", "automatic", False, False, None),
    ("scene", "turn_on", "automatic", False, False, None),
    ("script", "turn_on", "automatic", False, False, None),
    ("lock", "unlock", "chat", False, True, None),
    ("lock", "unlock", "panel", False, True, None),
    ("lock", "unlock", "chat", True, True, "spoken"),
    ("lock", "unlock", "voice", False, True, "phone"),
    ("cover", "open_cover", "voice", False, True, "phone"),
    ("alarm_control_panel", "alarm_disarm", "voice", False, True, "phone"),
    ("alarm_control_panel", "alarm_disarm", "voice", True, True, "phone"),
    ("alarm_control_panel", "alarm_disarm", "chat", True, True, "spoken"),
    ("alarm_control_panel", "alarm_disarm", "chat", False, True, None),
    ("lock", "lock", "voice", False, True, None),
    ("nova", "dismiss_intrusion", "chat", False, True, "spoken"),
    ("nova", "dismiss_intrusion", "voice", False, True, "phone"),
]


@pytest.mark.parametrize("domain,service,source,confirm_on,allowed,asked", TABLE)
async def test_action_by_source(pol, fake_hass, domain, service, source, confirm_on,
                                allowed, asked):
    p, fake = pol
    fake.enabled = confirm_on
    d = await p.authorize(fake_hass, _req(p, domain, service, source))
    assert d.allowed is allowed, d
    assert [how for how, _q in fake.asked] == ([asked] if asked else [])
    if source == "automatic" and not allowed:
        assert d.approval == "denied"


@pytest.mark.parametrize("answer", ["rejected", "expired"])
async def test_a_refused_phone_tap_blocks_the_disarm(pol, fake_hass, answer):
    p, fake = pol
    fake.phone_answer = answer
    d = await p.authorize(fake_hass, _req(p, "alarm_control_panel", "alarm_disarm", "voice"))
    assert not d.allowed and d.approval == answer
    assert "disarm the alarm" in fake.asked[0][1]


@pytest.mark.parametrize("confirm_on", [False, True])
async def test_disarm_from_a_voice_satellite_needs_a_phone_tap(pol, fake_hass, confirm_on):
    p, fake = pol
    fake.enabled = confirm_on
    fake.satellites.add("sat1")
    fake.phone_answer = "rejected"
    d = await p.authorize(fake_hass, p.AuthorityRequest(
        "alarm_control_panel", "alarm_disarm", "alarm_control_panel.home", device_id="sat1"))
    assert not d.allowed and [how for how, _ in fake.asked] == ["phone"]


@pytest.mark.parametrize("confirm_on", [False, True])
def test_disarm_by_voice_is_held_where_nothing_can_be_asked(pol, fake_hass, confirm_on):
    p, fake = pol
    fake.enabled = confirm_on
    held = p.authorize_now(fake_hass, _req(p, "alarm_control_panel", "alarm_disarm", "voice"))
    assert not held.allowed and held.approval == "deferred"
    ok = p.authorize_now(fake_hass, _req(p, "lock", "lock", "voice"))
    assert ok.allowed


async def test_it_fails_closed(pol, fake_hass, monkeypatch, load):
    p, _ = pol

    def boom(*a, **k):
        raise RuntimeError("confirmation layer down")
    monkeypatch.setattr(load("voice_confirm"), "action_is_protected", boom)
    monkeypatch.setattr(p, "confirm_gate", boom)
    d = await p.authorize(fake_hass, _req(p, "lock", "unlock", "chat"))
    assert not d.allowed and d.approval == "error"
    assert (await p.authorize(fake_hass, _req(p, "light", "turn_on", "chat"))).allowed
    monkeypatch.setattr(p, "requires_confirmation", boom)
    assert not p.authorize_now(fake_hass, _req(p, "lock", "unlock", "chat")).allowed
    assert p.authorize_now(fake_hass, _req(p, "light", "turn_on", "chat")).allowed


# ── the voice "secure" reply and the offers executor ────────────────────────

@pytest.fixture
def al(load):
    return load("action_log")


def _router(load, fake_hass):
    ir = load("intent.intent_router")
    r = ir.LocalIntentRouter(fake_hass)
    r._area_of = lambda eid: "hall"
    return r


async def test_the_voice_secure_reply_locks_and_closes_and_logs(load, fake_hass, al, pol):
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    r = _router(load, fake_hass)
    out = await r.execute({"intent": "secure_area"}, "hall")
    assert out["executed"] and sorted(out["entities"]) == ["cover.garage", "lock.front"]
    rows = [t for p_ in al.page_requests()["requests"] for t in p_["targets"]]
    assert {(t["entity_id"], t["execution_result"]) for t in rows} == {
        ("cover.garage", "accepted"), ("lock.front", "accepted")}
    assert {p_["source"] for p_ in al.page_requests()["requests"]} == {"voice"}


async def test_the_voice_reply_holds_a_device_that_needs_confirmation(
        load, fake_hass, al, pol, monkeypatch):
    p, fake = pol
    fake.enabled = True
    monkeypatch.setattr(load("voice_confirm"), "action_is_protected",
                        lambda hass, d, s, e="": e == "lock.front")
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("lock.back", "unlocked")
    r = _router(load, fake_hass)
    out = await r.execute({"intent": "secure_area"}, "hall")
    assert out["entities"] == ["lock.back"]
    rows = {t["entity_id"]: t["execution_result"]
            for p_ in al.page_requests()["requests"] for t in p_["targets"]}
    assert rows == {"lock.back": "accepted", "lock.front": "blocked"}
    assert [c[2]["entity_id"] for c in fake_hass.service_calls] == [["lock.back"]]


async def test_an_offer_nova_runs_on_its_own_may_not_unlock(load, fake_hass, al, pol):
    cc = load("cognitive_core")
    ok = await cc._execute_action_data(
        fake_hass, {"domain": "lock", "service": "unlock", "entity_ids": ["lock.front"]},
        source="proactive_autonomous")
    assert ok is False and fake_hass.service_calls == []
    rows = [t for p_ in al.page_requests()["requests"] for t in p_["targets"]]
    assert rows[0]["execution_result"] == "blocked"
    assert await cc._execute_action_data(
        fake_hass, {"domain": "light", "service": "turn_off", "entity_ids": ["light.porch"]},
        source="proactive_autonomous") is True
