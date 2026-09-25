"""Assist policy bridge (assist_policy.py): Home Assistant's Assist tools go
through Nova's policy gate before hass_api.async_call_tool runs them.

Fakes only. No real lock, alarm, cover or garage entity is ever touched: the
Home Assistant intent helper, its handlers, the Assist tools and the API
instance are all small stand-ins, and the confirmation layer is either a spy
or the real policy.py with voice_confirm stubbed.

Focused run:
    python -m pytest tests/unit/test_assist_policy.py -q
"""
import json
import types

import pytest


# ── Fakes: Home Assistant's intent helper, tools and API instance ───────────

class _Constraints:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def has_constraints(self):
        return bool(self.name or self.area_name or self.floor_name
                    or self.domains or self.device_classes)


class _FakeIntentHelper:
    """Stands in for homeassistant.helpers.intent. ``entities`` maps an
    entity_id to its friendly name; matching is by name and domain only."""

    MatchTargetsConstraints = _Constraints

    def __init__(self, handlers, entities):
        self._handlers = handlers
        self._entities = entities
        self.match_calls = []

    def async_get(self, hass):
        return list(self._handlers)

    @staticmethod
    def is_blank_slot_value(v):
        return v is None or v == ""

    def async_match_targets(self, hass, constraints):
        self.match_calls.append(constraints)
        states = []
        for eid, fname in self._entities.items():
            dom = eid.split(".", 1)[0]
            if constraints.domains and dom not in constraints.domains:
                continue
            if constraints.name and constraints.name.lower() != fname.lower():
                continue
            states.append(types.SimpleNamespace(entity_id=eid, domain=dom))
        return types.SimpleNamespace(is_match=bool(states), states=states)


class _ServiceHandler:
    """Mimics intent.ServiceIntentHandler: validates name/area/floor slots
    and reports a fixed (domain, service) for every target."""

    required_domains = None
    required_features = None
    required_states = None

    def __init__(self, intent_type, domain="homeassistant", service="turn_on"):
        self.intent_type = intent_type
        self._ds = (domain, service)

    def async_validate_slots(self, slots):
        if not any(k in slots for k in ("name", "area", "floor")):
            raise ValueError("name, area or floor required")
        for key in ("name", "area", "floor"):
            if key in slots and not isinstance(slots[key]["value"], str):
                raise ValueError(f"{key} must be a string")
        return slots

    def get_domain_and_service(self, intent_obj, state):
        return self._ds


class IntentTool:
    def __init__(self, intent_type):
        self.name = intent_type
        self.intent_type = intent_type


class ScriptTool:
    def __init__(self, object_id):
        self.name = object_id
        self._domain = "script"
        self._action = object_id


class GetLiveContextTool:
    name = "GetLiveContext"


class MysteryTool:
    name = "do_something_new"


class _Api:
    def __init__(self, tools):
        self.tools = tools
        self.llm_context = types.SimpleNamespace(assistant="conversation")
        self.calls = []

    async def async_call_tool(self, tool_input):
        self.calls.append(tool_input)
        return {"ok": True, "tool": tool_input.tool_name}


class _Hass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


ENTITIES = {
    "light.kitchen": "Kitchen",
    "lock.front_door": "Front door",
    "cover.garage_door": "Garage door",
    "alarm_control_panel.home": "Alarm",
    "scene.movie": "Movie",
    "switch.fan": "Fan",
}

HANDLERS = [
    _ServiceHandler("HassTurnOn", "homeassistant", "turn_on"),
    _ServiceHandler("HassTurnOff", "homeassistant", "turn_off"),
    _ServiceHandler("HassToggle", "homeassistant", "toggle"),
]


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def ap(load):
    return load("assist_policy")


@pytest.fixture
def pol(load):
    return load("policy")


@pytest.fixture
def helper(ap, monkeypatch):
    h = _FakeIntentHelper(HANDLERS, ENTITIES)
    monkeypatch.setattr(ap, "_intent_helper", lambda: h)
    monkeypatch.setattr(ap, "_script_entity_id", lambda hass, action: f"script.{action}")
    return h


@pytest.fixture
def log(load, monkeypatch):
    """Capture action_log writes instead of touching a database."""
    al = load("action_log")
    vc = load("voice_confirm")
    rec = {"rows": [], "executions": []}

    def _start_many(request_id, action, source, targets, **kw):
        rec["rows"].append({"action": action, "source": source, "targets": targets, **kw})
        return {t["key"]: 100 + i for i, t in enumerate(targets)}

    monkeypatch.setattr(al, "start_many", _start_many)
    monkeypatch.setattr(al, "set_execution",
                        lambda aid, outcome, **kw: rec["executions"].append((aid, outcome)))
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, d, **k: d == "satellite")
    return rec


@pytest.fixture
def gate(pol, monkeypatch):
    """Spy on policy.confirm_gate. Set ``gate.outcome`` to force a result."""
    spy = types.SimpleNamespace(calls=[], outcome=None)

    async def _confirm_gate(hass, domain, service, entity_id="", action_label="", device_id=""):
        spy.calls.append((domain, service, entity_id, device_id))
        if spy.outcome is None:
            risk, _ = pol.classify(domain, service, entity_id)
            return True, "", "not_required" if risk == "low" else "approved"
        ok = spy.outcome in ("approved", "not_required")
        return ok, "" if ok else f"not confirmed ({spy.outcome})", spy.outcome

    monkeypatch.setattr(pol, "confirm_gate", _confirm_gate)
    return spy


def _api(*tools):
    return _Api(list(tools) or [IntentTool("HassTurnOn"), IntentTool("HassTurnOff")])


async def _authorize(ap, api, name, args, device_id=""):
    return await ap.async_authorize(_Hass(), api, name, args, device_id=device_id)


# ── 1, 14: safe light executes exactly once through the agent path ──────────

async def test_safe_light_executes_exactly_once(load, helper, gate, log):
    agent = load("agent")
    api = _api()
    user_input = types.SimpleNamespace(device_id=None, context=None, text="", language="en")
    out = await agent._execute_tool(_Hass(), "HassTurnOn", {"name": "Kitchen"}, api, user_input)
    assert json.loads(out)["ok"] is True
    assert len(api.calls) == 1
    assert gate.calls == [("light", "turn_on", "light.kitchen", "")]
    assert log["executions"] == [(100, "accepted")]


# ── 2: locking goes through the adapter and runs when allowed ───────────────

async def test_lock_routes_through_adapter_and_is_allowed(ap, helper, gate, log):
    d = await _authorize(ap, _api(), "HassTurnOn", {"name": "Front door"})
    assert d.allowed
    assert gate.calls == [("lock", "lock", "lock.front_door", "")]
    assert d.classification.targets[0].risk == "low"


# ── 3: unlocking from text is sent through confirm_gate ─────────────────────

async def test_text_unlock_goes_through_confirm_gate(ap, helper, gate, log):
    gate.outcome = "rejected"
    d = await _authorize(ap, _api(), "HassTurnOff", {"name": "Front door"})
    assert gate.calls == [("lock", "unlock", "lock.front_door", "")]
    assert d.classification.kind == ap.PROTECTED_MUTATION
    assert d.classification.targets[0].risk == "high"
    assert not d.allowed


# ── 4: voice unlock can't run without a phone tap (real policy.py) ──────────

@pytest.mark.parametrize("phone", ["expired", "rejected", "deferred"])
async def test_voice_unlock_needs_phone_approval(ap, pol, load, helper, log, monkeypatch, phone):
    vc = load("voice_confirm")
    asked = []

    async def _phone_only(hass, question):
        asked.append(question)
        return phone

    async def _spoken(*a, **k):
        raise AssertionError("spoken confirmation must never authorize an unlock")

    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", _phone_only)
    monkeypatch.setattr(vc, "confirm_typed", _spoken)
    api = _api()
    d = await _authorize(ap, api, "HassTurnOff", {"name": "Front door"}, device_id="satellite")
    assert not d.allowed
    assert d.approval_result == phone
    assert len(asked) == 1
    assert api.calls == []
    assert log["rows"][0]["source"] == "voice"
    assert log["rows"][0]["targets"][0]["execution_result"] == "blocked"


async def test_voice_unlock_runs_after_phone_approval(ap, load, helper, log, monkeypatch):
    vc = load("voice_confirm")

    async def _phone_only(hass, question):
        return "approved"

    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", _phone_only)
    d = await _authorize(ap, _api(), "HassTurnOff", {"name": "Front door"}, device_id="satellite")
    assert d.allowed and d.approval_result == "approved"


# ── 5, 6: covers ────────────────────────────────────────────────────────────

async def test_open_cover_needs_approval(ap, helper, gate, log):
    gate.outcome = "expired"
    api = _api()
    d = await _authorize(ap, api, "HassTurnOn", {"name": "Garage door"})
    assert gate.calls == [("cover", "open_cover", "cover.garage_door", "")]
    assert not d.allowed
    assert api.calls == []


async def test_set_position_open_is_treated_as_open_cover(ap, helper, gate, log, monkeypatch):
    h = _FakeIntentHelper([_ServiceHandler("HassSetPosition", "cover", "set_cover_position")],
                          ENTITIES)
    monkeypatch.setattr(ap, "_intent_helper", lambda: h)
    gate.outcome = "rejected"
    d = await _authorize(ap, _api(IntentTool("HassSetPosition")), "HassSetPosition",
                         {"name": "Garage door", "position": 40})
    assert gate.calls == [("cover", "open_cover", "cover.garage_door", "")]
    assert not d.allowed


async def test_voice_open_cover_is_phone_only(ap, load, helper, log, monkeypatch):
    vc = load("voice_confirm")

    async def _phone_only(hass, question):
        return "expired"

    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", _phone_only)
    d = await _authorize(ap, _api(), "HassTurnOn", {"name": "Garage door"}, device_id="satellite")
    assert not d.allowed and d.approval_result == "expired"


async def test_close_cover_stays_low_friction(ap, helper, gate, log):
    d = await _authorize(ap, _api(), "HassTurnOff", {"name": "Garage door"})
    assert d.allowed
    assert gate.calls == [("cover", "close_cover", "cover.garage_door", "")]
    assert d.classification.kind == ap.SAFE_MUTATION
    assert d.approval_result == "not_required"


# ── 7, 8: alarm ─────────────────────────────────────────────────────────────

class _AlarmActionTool:
    def __init__(self, action):
        self.name = f"alarm_control_panel__{action}"
        self._domain = "alarm_control_panel"
        self._action = action


async def test_possible_disarm_is_critical(ap, helper, gate, log):
    gate.outcome = "rejected"
    tool = _AlarmActionTool("alarm_disarm")
    d = await _authorize(ap, _api(tool), tool.name, {"entity_id": "alarm_control_panel.home"})
    assert d.classification.targets[0].risk == "critical"
    assert gate.calls == [("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home", "")]
    assert not d.allowed


@pytest.mark.parametrize("intent", ["HassTurnOff", "HassTurnOn", "HassToggle"])
async def test_ambiguous_alarm_intent_fails_closed(ap, helper, gate, log, intent):
    api = _api(IntentTool(intent))
    d = await _authorize(ap, api, intent, {"name": "Alarm"})
    assert not d.allowed
    assert d.classification.kind == ap.UNKNOWN_MUTATION
    assert d.classification.targets[0].risk == "critical"
    assert gate.calls == []
    assert api.calls == []


async def test_unknown_alarm_action_fails_closed(ap, helper, gate, log):
    tool = _AlarmActionTool("alarm_mystery")
    d = await _authorize(ap, _api(tool), tool.name, {"entity_id": "alarm_control_panel.home"})
    assert not d.allowed and d.classification.kind == ap.UNKNOWN_MUTATION
    assert gate.calls == []


# ── 9, 10: scripts and scenes are indirect actuation ────────────────────────

async def test_script_tool_goes_through_indirection_policy(ap, helper, gate, log):
    gate.outcome = "rejected"
    tool = ScriptTool("goodnight")
    d = await _authorize(ap, _api(tool), "goodnight", {})
    assert gate.calls == [("script", "turn_on", "script.goodnight", "")]
    assert d.classification.kind == ap.PROTECTED_MUTATION
    assert d.classification.targets[0].risk == "medium"
    assert not d.allowed


async def test_scene_activation_goes_through_indirection_policy(ap, helper, gate, log):
    d = await _authorize(ap, _api(), "HassTurnOn", {"name": "Movie"})
    assert gate.calls == [("scene", "turn_on", "scene.movie", "")]
    assert d.classification.kind == ap.PROTECTED_MUTATION
    assert d.allowed and d.approval_result == "approved"


# ── 11, 12: malformed and unknown mutations never reach Home Assistant ──────

@pytest.mark.parametrize("args", [{}, {"name": 5}, "not a dict", {"area": ["x"]}])
async def test_malformed_mutation_is_blocked(ap, helper, gate, log, args):
    api = _api()
    d = await _authorize(ap, api, "HassTurnOff", args)
    assert not d.allowed
    assert d.classification.kind == ap.MALFORMED
    assert d.tool_result()["status"] == "invalid_request"
    assert gate.calls == [] and api.calls == []


async def test_malformed_position_is_blocked(ap, helper, gate, log, monkeypatch):
    h = _FakeIntentHelper([_ServiceHandler("HassSetPosition", "cover", "set_cover_position")],
                          ENTITIES)
    monkeypatch.setattr(ap, "_intent_helper", lambda: h)
    d = await _authorize(ap, _api(IntentTool("HassSetPosition")), "HassSetPosition",
                         {"name": "Garage door", "position": 150})
    assert not d.allowed and d.classification.kind == ap.MALFORMED


async def test_unknown_mutating_tool_is_blocked(ap, helper, gate, log):
    api = _api(MysteryTool())
    d = await _authorize(ap, api, "do_something_new", {"target": "front door"})
    assert not d.allowed
    assert d.classification.kind == ap.UNKNOWN_MUTATION
    assert d.tool_result()["status"] == "blocked_unclassified"
    assert log["rows"][0]["targets"][0]["execution_result"] == "blocked"


async def test_unknown_intent_is_blocked(ap, helper, gate, log):
    api = _api(IntentTool("HassOpenTheVault"))
    d = await _authorize(ap, api, "HassOpenTheVault", {"name": "Vault"})
    assert not d.allowed and d.classification.kind == ap.UNKNOWN_MUTATION


async def test_unresolved_target_is_blocked(ap, helper, gate, log):
    d = await _authorize(ap, _api(), "HassTurnOff", {"name": "Nothing by that name"})
    assert not d.allowed and d.classification.kind == ap.UNKNOWN_MUTATION
    assert gate.calls == []


async def test_intent_without_registered_handler_is_blocked(ap, gate, log, monkeypatch):
    monkeypatch.setattr(ap, "_intent_helper", lambda: _FakeIntentHelper([], ENTITIES))
    d = await _authorize(ap, _api(), "HassTurnOff", {"name": "Front door"})
    assert not d.allowed and d.classification.kind == ap.UNKNOWN_MUTATION


async def test_tool_not_offered_by_home_assistant_is_blocked(ap, helper, gate, log):
    d = await _authorize(ap, _api(), "HassUnlockEverything", {"name": "x"})
    assert not d.allowed


async def test_internal_failure_denies(ap, helper, gate, log, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ap, "_find_tool", _boom)
    d = await _authorize(ap, _api(), "HassTurnOff", {"name": "Front door"})
    assert not d.allowed and d.approval_result == "error"


# ── 13: every non-approval outcome stops execution (agent path) ─────────────

@pytest.mark.parametrize("outcome", ["rejected", "expired", "deferred", "error"])
async def test_non_approved_outcomes_do_not_execute(load, helper, gate, log, outcome):
    agent = load("agent")
    gate.outcome = outcome
    api = _api()
    user_input = types.SimpleNamespace(device_id=None, context=None, text="", language="en")
    out = json.loads(await agent._execute_tool(
        _Hass(), "HassTurnOff", {"name": "Front door"}, api, user_input))
    assert out["status"] == "awaiting_confirmation"
    assert api.calls == []
    assert log["rows"][0]["targets"][0]["approval_result"] == outcome
    assert log["executions"] == []


async def test_approved_protected_operation_executes_once(load, helper, gate, log):
    agent = load("agent")
    gate.outcome = "approved"
    api = _api()
    user_input = types.SimpleNamespace(device_id=None, context=None, text="", language="en")
    await agent._execute_tool(_Hass(), "HassTurnOff", {"name": "Front door"}, api, user_input)
    assert len(api.calls) == 1
    assert log["rows"][0]["targets"][0]["approval_result"] == "approved"
    assert log["executions"] == [(100, "accepted")]


# ── 15: the same HA API instance and its context are reused ─────────────────

async def test_same_api_instance_and_context_are_used(load, helper, gate, log):
    agent = load("agent")
    api = _api()
    context = types.SimpleNamespace(user_id="user-1")
    user_input = types.SimpleNamespace(device_id="kitchen-display", context=context,
                                       text="", language="en")
    await agent._execute_tool(_Hass(), "HassTurnOn", {"name": "Kitchen"}, api, user_input)
    assert len(api.calls) == 1
    assert api.calls[0].tool_name == "HassTurnOn"
    assert api.calls[0].tool_args == {"name": "Kitchen"}
    # The request's device reaches Nova's gate, and the user is recorded.
    assert gate.calls[0][3] == "kitchen-display"
    assert log["rows"][0]["requested_by_user_id"] == "user-1"
    # Resolution used HA's own exposure check for this assistant.
    assert helper.match_calls[0].assistant == "conversation"
    assert helper.match_calls[0].allow_duplicate_names is True


# ── 16: read-only Assist tools keep working ─────────────────────────────────

@pytest.mark.parametrize("tool", [IntentTool("HassGetState"), GetLiveContextTool()])
async def test_read_only_tools_pass_without_gate(load, helper, gate, log, tool):
    agent = load("agent")
    api = _api(tool)
    user_input = types.SimpleNamespace(device_id=None, context=None, text="", language="en")
    out = json.loads(await agent._execute_tool(_Hass(), tool.name, {}, api, user_input))
    assert out["ok"] is True
    assert gate.calls == [] and log["rows"] == []


async def test_annotated_read_only_tool_passes(ap, helper, gate, log):
    class NewReadTool:
        name = "future_lookup"
        annotations = types.SimpleNamespace(read_only=True)

    d = await _authorize(ap, _api(NewReadTool()), "future_lookup", {})
    assert d.allowed and d.classification.kind == ap.READ_ONLY


async def test_unannotated_unknown_tool_is_not_assumed_read_only(ap, helper, gate, log):
    class NewWriteTool:
        name = "future_write"
        annotations = types.SimpleNamespace(read_only=False)

    d = await _authorize(ap, _api(NewWriteTool()), "future_write", {})
    assert not d.allowed


async def test_namespaced_tool_is_unwrapped(ap, helper, gate, log):
    class NamespacedTool:
        def __init__(self, inner):
            self.name = f"assist__{inner.name}"
            self.tool = inner

    api = _api(NamespacedTool(IntentTool("HassTurnOff")))
    gate.outcome = "rejected"
    d = await _authorize(ap, api, "assist__HassTurnOff", {"name": "Front door"})
    assert gate.calls == [("lock", "unlock", "lock.front_door", "")]
    assert not d.allowed


async def test_older_core_intent_tool_uses_its_own_name(ap, helper, gate, log):
    """HA 2026.2's IntentTool has no intent_type attribute; it dispatches
    intent_type=self.name, so the adapter must read the name for that class."""
    class IntentTool:  # same class name as the real one, no intent_type
        def __init__(self, name):
            self.name = name

    gate.outcome = "rejected"
    api = _api(IntentTool("HassTurnOff"))
    d = await _authorize(ap, api, "HassTurnOff", {"name": "Front door"})
    assert gate.calls == [("lock", "unlock", "lock.front_door", "")]
    assert not d.allowed and api.calls == []


async def test_nameonly_tool_of_other_class_is_not_treated_as_intent(ap, helper, gate, log):
    class OtherTool:
        name = "HassTurnOff"

    d = await _authorize(ap, _api(OtherTool()), "HassTurnOff", {"name": "Front door"})
    assert not d.allowed and d.classification.kind == ap.UNKNOWN_MUTATION
    assert gate.calls == []


# ── 17: Nova-native tools keep their own path ───────────────────────────────

async def test_native_tools_do_not_use_the_assist_adapter(load, ap, monkeypatch):
    agent = load("agent")
    seen = []

    async def _native(hass, args):
        seen.append(args)
        return "native"

    async def _no(*a, **k):
        raise AssertionError("assist adapter must not run for native tools")

    monkeypatch.setitem(agent._TOOL_MAP, "get_home_summary", _native)
    monkeypatch.setattr(ap, "async_authorize", _no)
    out = await agent._execute_tool(_Hass(), "get_home_summary", {}, _api(), None)
    assert out == "native" and seen == [{}]


# ── Security-hint switches and pure classification ──────────────────────────

async def test_security_switch_turn_off_is_protected(ap, gate, log, monkeypatch):
    h = _FakeIntentHelper(HANDLERS, {"switch.alarm_siren": "Siren"})
    monkeypatch.setattr(ap, "_intent_helper", lambda: h)
    d = await _authorize(ap, _api(), "HassTurnOff", {"name": "Siren"})
    assert d.classification.targets[0].risk == "high"
    assert d.classification.kind == ap.PROTECTED_MUTATION


def test_classify_operation_is_pure_and_uses_policy(ap):
    desc = ap.ToolDescriptor(ap.TOOL_INTENT, "HassTurnOff", intent_type="HassTurnOff",
                             resolvable=True)
    c = ap.classify_operation(desc, {"name": "Front door"}, [("lock.front_door", None)])
    assert [(t.domain, t.service, t.risk) for t in c.targets] == [("lock", "unlock", "high")]
    c = ap.classify_operation(desc, {"name": "x"}, None)
    assert c.kind == ap.UNKNOWN_MUTATION


def test_toggle_maps_to_guard_dropping_direction(ap):
    desc = ap.ToolDescriptor(ap.TOOL_INTENT, "HassToggle", intent_type="HassToggle",
                             resolvable=True)
    c = ap.classify_operation(desc, {"name": "x"},
                              [("lock.a", None), ("cover.b", None)])
    assert [(t.domain, t.service) for t in c.targets] == [("lock", "unlock"),
                                                         ("cover", "open_cover")]
