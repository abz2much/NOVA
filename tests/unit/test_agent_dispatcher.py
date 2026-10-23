"""Tool dispatch, the agent's security boundary: what is refused before
anything runs, how a Nova tool is called, how Home Assistant's own tools are
reached behind Nova's policy, and the exact error texts."""
import json
import logging
import types

import pytest

from fakes import FakeHass

SAFE = "RuntimeError (details are in the Home Assistant log)"


@pytest.fixture
def dp(load):
    return load("agent_runtime.dispatcher")


@pytest.fixture
def gr(load):
    return load("agent_runtime.grants")


@pytest.fixture
def md(load):
    return load("agent_runtime.models")


def _call(name="get_entity_state", args=None):
    return types.SimpleNamespace(name=name, args={} if args is None else args)


# ── precheck ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("call", [
    types.SimpleNamespace(name="", args={}),
    types.SimpleNamespace(name=None, args={}),
    types.SimpleNamespace(name=5, args={}),
    types.SimpleNamespace(name="x", args=None),
    types.SimpleNamespace(name="x", args=["a"]),
    types.SimpleNamespace(name="x", args="text"),
    types.SimpleNamespace(args={}),
    types.SimpleNamespace(name="x"),
])
def test_a_malformed_call_is_refused_with_these_words(dp, gr, call):
    out = dp.precheck(call, grant=gr.MAIN_GRANT, defer_mutating=False)
    assert out.is_error and out.code == "malformed_call"
    assert json.loads(out.content) == {
        "error": "the tool call was malformed (it needs a tool name and an object "
                 "of arguments) and was not run"}


def test_any_mapping_counts_as_arguments(dp, gr):
    assert dp.precheck(_call(args=types.MappingProxyType({"a": 1})),
                       grant=gr.MAIN_GRANT, defer_mutating=False) is None


@pytest.mark.parametrize("grant_name, message", [
    ("delegated", "tool 'control_device' is not available to this sub-agent — it was not granted"),
    ("headless", "tool 'control_device' is not available to a scheduled run — it can "
                 "check and report, not act or change anything"),
    ("scoped", "tool 'control_device' is not available here — it was not granted"),
    ("anything", "tool 'control_device' is not available here — it was not granted"),
])
def test_a_tool_outside_the_grant_is_refused_in_the_words_for_that_kind_of_run(
        dp, md, grant_name, message):
    grant = md.ToolGrant(grant_name, frozenset({"get_entity_state"}))
    out = dp.precheck(_call("control_device"), grant=grant, defer_mutating=False)
    assert out.is_error and out.code == "not_granted"
    assert json.loads(out.content) == {"error": message}
    assert dp._not_granted_message("control_device", grant) == message


def test_a_granted_tool_is_let_through(dp, md):
    grant = md.ToolGrant("scoped", frozenset({"get_entity_state"}))
    assert dp.precheck(_call(), grant=grant, defer_mutating=True) is None


def test_the_grant_is_checked_before_the_deferral(dp, md):
    grant = md.ToolGrant("headless", frozenset({"get_entity_state"}))
    out = dp.precheck(_call("control_device"), grant=grant, defer_mutating=True)
    assert out.code == "not_granted"


def test_a_malformed_call_is_refused_before_the_grant_is_looked_at(dp, md):
    grant = md.ToolGrant("scoped", frozenset())
    out = dp.precheck(types.SimpleNamespace(name="control_device", args=None),
                      grant=grant, defer_mutating=True)
    assert out.code == "malformed_call"


@pytest.mark.parametrize("name", ["control_device", "bulk_control", "run_scene_or_script",
                                  "execute_plan", "run_voice_phrase"])
def test_a_mutating_tool_waits_for_an_unresolved_unique_search(dp, gr, name):
    out = dp.precheck(_call(name), grant=gr.MAIN_GRANT, defer_mutating=True)
    assert out.code == "deferred" and out.is_error is False
    assert json.loads(out.content) == {
        "deferred": True,
        "reason": "Resolve the exact entity with search_entities(require_unique=true) "
                  "first, then repeat this action with the resolved entity_id."}
    assert list(json.loads(out.content)) == ["deferred", "reason"]


def test_deferral_only_applies_when_asked_and_only_to_mutating_tools(dp, gr):
    assert dp.precheck(_call("control_device"), grant=gr.MAIN_GRANT,
                       defer_mutating=False) is None
    assert dp.precheck(_call("get_entity_state"), grant=gr.MAIN_GRANT,
                       defer_mutating=True) is None
    assert dp.precheck(_call("some_ha_tool"), grant=gr.MAIN_GRANT,
                       defer_mutating=True) is None


# ── execute ──────────────────────────────────────────────────────────────────

async def test_execute_runs_the_tool_and_types_the_result(dp, md, monkeypatch):
    seen = []

    async def fake(hass, name, args, hass_api, user_input, *, ctx):
        seen.append((hass, name, args, hass_api, user_input, ctx))
        return json.dumps({"error": "nope"})

    monkeypatch.setattr(dp, "_execute_tool", fake)
    api, ui = object(), object()
    ctx = md.ToolExecutionContext(hass_api=api, user_input=ui)
    hass = FakeHass()
    call = _call("web_research", {"query": "q"})
    result = await dp.execute(hass, call, ctx)
    assert seen == [(hass, "web_research", {"query": "q"}, api, ui, ctx)]
    assert seen[0][2] is not call.args
    assert result.is_error and result.code == "tool_error"
    assert result.trust is md.Trust.UNTRUSTED_EXTERNAL


async def test_a_household_tool_result_is_household_trust(dp, md, monkeypatch):
    async def fake(*a, **k):
        return json.dumps({"ok": True})
    monkeypatch.setattr(dp, "_execute_tool", fake)
    result = await dp.execute(FakeHass(), _call("control_device"),
                              md.ToolExecutionContext())
    assert result.is_error is False and result.code is None
    assert result.trust is md.Trust.HOUSEHOLD_DATA


# ── Nova's own tools ─────────────────────────────────────────────────────────

@pytest.fixture
def nova_tool(dp, monkeypatch):
    calls = []

    def install(fn, name="probe"):
        async def wrapper(*a, **k):
            return await fn(*a, **k)
        wrapper.__signature__ = __import__("inspect").signature(fn)
        monkeypatch.setitem(dp._TOOL_MAP, name, fn)
        return calls
    return install


async def test_a_plain_tool_gets_only_the_hass_and_the_arguments(dp, md, nova_tool):
    got = []

    async def probe(hass, args):
        got.append((hass, args))
        return "plain text"

    nova_tool(probe)
    hass = FakeHass()
    ctx = md.ToolExecutionContext(device_id="dev", user_id="u")
    assert await dp._execute_tool(hass, "probe", {"a": 1}, ctx=ctx) == "plain text"
    assert got == [(hass, {"a": 1})]


async def test_a_tool_that_asks_for_the_requester_gets_it(dp, md, nova_tool):
    got = {}

    async def probe(hass, args, device_id=None, user_id=None, identification=None):
        got.update(device_id=device_id, user_id=user_id, identification=identification)
        return "x"

    nova_tool(probe)
    who = object()
    ctx = md.ToolExecutionContext(device_id="dev", user_id="u", identification=who)
    await dp._execute_tool(FakeHass(), "probe", {}, ctx=ctx)
    assert got == {"device_id": "dev", "user_id": "u", "identification": who}


async def test_a_tool_gets_only_the_requester_fields_it_names(dp, md, nova_tool):
    got = {}

    async def probe(hass, args, *, user_id=None):
        got["user_id"] = user_id
        return "x"

    nova_tool(probe)
    await dp._execute_tool(FakeHass(), "probe", {}, ctx=md.ToolExecutionContext(
        device_id="dev", user_id="u"))
    assert got == {"user_id": "u"}


async def test_nothing_is_invented_when_the_request_has_no_requester(dp, md, nova_tool):
    got = {}

    async def probe(hass, args, device_id="d", user_id="u"):
        got.update(device_id=device_id, user_id=user_id)
        return "x"

    nova_tool(probe)
    await dp._execute_tool(FakeHass(), "probe", {}, ctx=md.ToolExecutionContext())
    assert got == {"device_id": None, "user_id": None}


async def test_without_a_context_it_is_built_from_the_user_input(dp, nova_tool):
    got = {}

    async def probe(hass, args, device_id=None, user_id=None):
        got.update(device_id=device_id, user_id=user_id)
        return "x"

    nova_tool(probe)
    user_input = types.SimpleNamespace(
        device_id="dev", context=types.SimpleNamespace(user_id="u"))
    await dp._execute_tool(FakeHass(), "probe", {}, None, user_input)
    assert got == {"device_id": "dev", "user_id": "u"}
    got.clear()
    await dp._execute_tool(FakeHass(), "probe", {})
    assert got == {"device_id": None, "user_id": None}


async def test_a_tool_whose_signature_cannot_be_read_gets_no_extras(dp, md, monkeypatch):
    got = []

    class Odd:
        @property
        def __signature__(self):
            raise ValueError("no signature")

        async def __call__(self, hass, args, **extra):
            got.append(extra)
            return "ok"

    monkeypatch.setitem(dp._TOOL_MAP, "odd", Odd())
    out = await dp._execute_tool(FakeHass(), "odd", {}, ctx=md.ToolExecutionContext(
        device_id="d", user_id="u"))
    assert out == "ok" and got == [{}]


async def test_a_failing_tool_gives_a_safe_error_and_logs_the_detail(dp, md, nova_tool, caplog):
    async def probe(hass, args):
        raise RuntimeError("secret detail")

    nova_tool(probe)
    with caplog.at_level(logging.WARNING):
        out = await dp._execute_tool(FakeHass(), "probe", {}, ctx=md.ToolExecutionContext())
    assert json.loads(out) == {"error": SAFE}
    assert any("Nova: tool probe failed" == r.getMessage() for r in caplog.records)


async def test_a_validation_message_from_a_tool_is_kept(dp, md, nova_tool, load):
    err = load("safe_errors").NovaValidationError("entity_id is wrong")

    async def probe(hass, args):
        raise err

    nova_tool(probe)
    out = await dp._execute_tool(FakeHass(), "probe", {}, ctx=md.ToolExecutionContext())
    assert json.loads(out) == {"error": "entity_id is wrong"}


# ── Home Assistant's own tools ───────────────────────────────────────────────

class _Decision:
    def __init__(self, allowed=True, mutating=False, refusal=None):
        self.allowed = allowed
        self.classification = types.SimpleNamespace(mutating=mutating)
        self._refusal = refusal

    def tool_result(self):
        return self._refusal


class _Api:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    async def async_call_tool(self, tool_input):
        self.calls.append(tool_input)
        item = self.results.pop(0) if self.results else {"done": True}
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def ha(dp, load, monkeypatch):
    policy = load("assist_policy")
    box = types.SimpleNamespace(decision=_Decision(), authorized=[], recorded=[])

    async def authorize(hass, api, name, args, **kw):
        box.authorized.append((name, args, kw))
        return box.decision

    async def record(hass, decision, ok):
        box.recorded.append((decision, ok))

    monkeypatch.setattr(policy, "async_authorize", authorize)
    monkeypatch.setattr(policy, "async_record_execution", record)

    class ToolInput:
        def __init__(self, tool_name, tool_args, platform, context, user_prompt,
                     language, assistant, device_id):
            self.kw = dict(tool_name=tool_name, tool_args=tool_args, platform=platform,
                           context=context, user_prompt=user_prompt, language=language,
                           assistant=assistant, device_id=device_id)

    monkeypatch.setattr(dp.llm, "ToolInput", ToolInput, raising=False)
    return box


async def test_an_unknown_tool_with_no_ha_api_is_unknown(dp, md):
    out = await dp._execute_tool(FakeHass(), "mystery", {}, ctx=md.ToolExecutionContext())
    assert json.loads(out) == {"error": "Unknown tool: mystery"}


async def test_the_policy_is_asked_with_the_requester_before_ha_runs_anything(dp, md, ha):
    api = _Api()
    ctx = md.ToolExecutionContext(device_id="dev", user_id="u")
    await dp._execute_tool(FakeHass(), "HassTurnOn", {"name": "lamp"}, api, ctx=ctx)
    assert ha.authorized == [("HassTurnOn", {"name": "lamp"},
                              {"device_id": "dev", "user_id": "u"})]
    ha.authorized.clear()
    await dp._execute_tool(FakeHass(), "HassTurnOn", {}, api, ctx=md.ToolExecutionContext())
    assert ha.authorized == [("HassTurnOn", {}, {"device_id": "", "user_id": None})]


async def test_a_refused_assist_call_returns_the_refusal_and_never_reaches_ha(dp, md, ha):
    ha.decision = _Decision(allowed=False, refusal={"error": "needs confirmation"})
    api = _Api()
    out = await dp._execute_tool(FakeHass(), "HassTurnOn", {}, api,
                                 ctx=md.ToolExecutionContext())
    assert json.loads(out) == {"error": "needs confirmation"}
    assert api.calls == [] and ha.recorded == []


async def test_an_assist_call_gets_the_turn_context_and_is_recorded(dp, md, ha):
    api = _Api([{"speech": "done"}])
    user_input = types.SimpleNamespace(context="ctx", text="turn on the lamp",
                                       language="fr", device_id="dev-1")
    ctx = md.ToolExecutionContext(user_input=user_input, device_id="dev-1")
    out = await dp._execute_tool(FakeHass(), "HassTurnOn", {"name": "lamp"}, api,
                                 user_input, ctx=ctx)
    assert json.loads(out) == {"speech": "done"}
    (tool_input,) = api.calls
    assert tool_input.kw == {
        "tool_name": "HassTurnOn", "tool_args": {"name": "lamp"}, "platform": "nova",
        "context": "ctx", "user_prompt": "turn on the lamp", "language": "fr",
        "assistant": "conversation", "device_id": "dev-1"}
    assert ha.recorded == [(ha.decision, True)]


async def test_an_assist_call_with_no_user_input_uses_blank_values(dp, md, ha):
    api = _Api()
    await dp._execute_tool(FakeHass(), "HassTurnOn", {}, api, None,
                           ctx=md.ToolExecutionContext())
    assert api.calls[0].kw == {
        "tool_name": "HassTurnOn", "tool_args": {}, "platform": "nova", "context": None,
        "user_prompt": "", "language": "en", "assistant": "conversation",
        "device_id": None}


async def test_a_text_result_comes_back_as_text_and_a_dict_as_json(dp, md, ha):
    api = _Api(["plain", {"k": 1}, 5])
    ctx = md.ToolExecutionContext()
    outs = [await dp._execute_tool(FakeHass(), "T", {}, api, ctx=ctx) for _ in range(3)]
    assert outs == ["plain", '{"k": 1}', "5"]


async def test_a_read_that_fails_is_tried_up_to_three_times(dp, md, ha):
    api = _Api([RuntimeError("one"), RuntimeError("two"), {"ok": 1}])
    out = await dp._execute_tool(FakeHass(), "T", {}, api, ctx=md.ToolExecutionContext())
    assert json.loads(out) == {"ok": 1} and len(api.calls) == 3
    assert ha.recorded == [(ha.decision, True)]


async def test_a_read_that_always_fails_ends_with_a_safe_error_and_one_record(dp, md, ha):
    api = _Api([RuntimeError("a"), RuntimeError("b"), RuntimeError("c"), {"never": 1}])
    out = await dp._execute_tool(FakeHass(), "T", {}, api, ctx=md.ToolExecutionContext())
    assert json.loads(out) == {"error": f"T failed: {SAFE}"}
    assert len(api.calls) == 3 and dp.MAX_TOOL_RETRIES == 2
    assert ha.recorded == [(ha.decision, False)]


async def test_a_mutating_call_gets_one_attempt_only(dp, md, ha):
    ha.decision = _Decision(mutating=True)
    api = _Api([RuntimeError("acted maybe"), {"ok": 1}])
    out = await dp._execute_tool(FakeHass(), "HassTurnOn", {}, api,
                                 ctx=md.ToolExecutionContext())
    assert json.loads(out) == {"error": f"HassTurnOn failed: {SAFE}"}
    assert len(api.calls) == 1
    assert ha.recorded == [(ha.decision, False)]


async def test_a_nova_tool_wins_over_a_ha_tool_of_the_same_name(dp, md, ha, nova_tool):
    async def probe(hass, args):
        return "nova"

    nova_tool(probe)
    api = _Api()
    out = await dp._execute_tool(FakeHass(), "probe", {}, api, ctx=md.ToolExecutionContext())
    assert out == "nova" and api.calls == [] and ha.authorized == []


def test_the_agent_logger_keeps_the_name_it_always_had(dp):
    assert dp._LOGGER.name == "jc.agent"
