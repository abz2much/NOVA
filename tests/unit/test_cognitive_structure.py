"""Structural boundaries of Nova's cognitive architecture (Phase 8).

Static checks over the source, so a change that breaks a boundary fails
here even when no behaviour test happens to cover it:

  * the pure cognitive modules import nothing effectful (no Home Assistant,
    no database, no provider, no persona, no cache) and never write module
    state, call a service, speak, notify, schedule or sleep;
  * the reasoning cache is written from exactly one place, the coordinator,
    and only through cache_policy.may_cache;
  * the coordinator itself never speaks, notifies or calls a service —
    delivery stays behind the observer's existing output gate;
  * no new module gains a direct speech, notification or service-call path;
  * observer delivery routes every proactive announcement through
    output_gate.can_announce before it speaks;
  * pattern analysis stays out of the state-change hot path.

Focused run:
    python -m pytest tests/unit/test_cognitive_structure.py -q
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
COG = ROOT / "cognitive"

PURE = ["models.py", "evaluators.py", "arbitration.py", "provider.py", "cache_policy.py"]
ALLOWED_STDLIB = {"__future__", "re", "json", "math", "typing", "dataclasses",
                  "collections", "itertools", "functools", "statistics", "enum"}
ALLOWED_RELATIVE = {(1, "models"), (1, "evaluators"), (2, "const"), (1, "")}
FORBIDDEN_ATTRS = {
    "async_call", "async_add_executor_job", "async_create_task",
    "async_create_background_task", "async_track_time_interval", "remember", "note_hit",
    "note_cloud_call", "save", "execute_chat", "chat", "record", "save_activity",
    "record_announcement", "async_announce", "states", "services", "bus", "sleep",
    "connect", "execute", "commit", "write_text", "write_bytes", "mkdir",
}
FORBIDDEN_NAMES = {"hass", "open", "print", "exec", "eval", "__import__"}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_level_names(tree):
    names = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
    return names


def pure_violations(path) -> list:
    tree = _tree(path)
    bad = []
    module_names = _module_level_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_STDLIB:
                    bad.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if (node.level, node.module or "") not in ALLOWED_RELATIVE:
                    bad.append(f"from {'.' * node.level}{node.module or ''} import")
            elif (node.module or "").split(".")[0] not in ALLOWED_STDLIB:
                bad.append(f"from {node.module} import")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bad.append(f"{type(node).__name__.lower()} {node.names}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            bad.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            bad.append(node.id)
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
            bad.append("async code")
        elif isinstance(node, (ast.FunctionDef,)):
            for inner in ast.walk(node):
                targets = []
                if isinstance(inner, (ast.Assign,)):
                    targets = inner.targets
                elif isinstance(inner, (ast.AugAssign, ast.AnnAssign)):
                    targets = [inner.target]
                for t in targets:
                    base = t
                    while isinstance(base, (ast.Subscript, ast.Attribute)):
                        base = base.value
                    if isinstance(base, ast.Name) and base.id in module_names and t is not base:
                        bad.append(f"writes module state {base.id}")
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                        and inner.func.attr in ("append", "add", "update", "pop", "clear",
                                                "setdefault", "extend", "remove") \
                        and isinstance(inner.func.value, ast.Name) \
                        and inner.func.value.id in module_names:
                    bad.append(f"mutates module state {inner.func.value.id}")
    return sorted(set(bad))


@pytest.mark.parametrize("name", PURE)
def test_pure_cognitive_modules_have_no_effects(name):
    path = COG / name
    assert path.exists(), name
    assert pure_violations(path) == []


def _calls(tree, attr):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == attr:
                out.append(n)
    return out


def test_reasoning_cache_is_written_only_by_the_coordinator():
    writers = set()
    for p in sorted(ROOT.rglob("*.py")):
        tree = _tree(p)
        for call in _calls(tree, "remember"):
            f = call.func
            if isinstance(f, ast.Attribute) and getattr(f.value, "id", None) == "reasoning_cache":
                writers.add(str(p.relative_to(ROOT)))
    assert writers == {"cognitive/coordinator.py"}


def test_the_coordinator_learns_only_through_the_cache_policy():
    tree = _tree(COG / "coordinator.py")
    learners = [f for f in ast.walk(tree)
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _calls(f, "remember")]
    assert [f.name for f in learners] == ["_learn"]
    body = ast.unparse(learners[0])
    assert "cache_policy.may_cache(decision)" in body
    assert body.index("may_cache") < body.index("remember")


def test_the_coordinator_never_speaks_notifies_or_calls_a_service():
    tree = _tree(COG / "coordinator.py")
    for attr in ("async_call", "async_announce", "can_announce", "record_announcement",
                 "async_send_configured_notifications", "confirm_gate"):
        assert _calls(tree, attr) == [], attr


# Modules with a direct Home Assistant service call or TTS announcement at
# v7.119.0. A module outside this set gaining one is a new delivery or
# actuator path and must be reviewed against policy and the output gate.
SERVICE_CALLERS = {
    "__init__.py", "agent_runtime/capabilities/control.py",
    "agent_runtime/capabilities/environment.py", "automation/installation.py",
    "cognitive_core.py", "continued_conversation.py", "intent/intent_router.py",
    "local_engine.py", "mode_scene.py", "notify_targets.py", "observer.py",
    "proactive_audio.py", "routines.py", "scenes.py", "services.py", "tts_helper.py",
    "voice_confirm.py",
}
ANNOUNCERS = {
    "__init__.py", "appliance_monitor.py", "briefing.py", "camera.py", "cognitive_core.py",
    "conversation.py", "hazard_monitor.py", "host_health.py", "package_monitor.py",
    "proactive_briefing.py", "reminders.py", "routines.py", "scenes.py", "sentinel.py",
    "services.py", "summary.py", "voice_confirm.py", "websocket.py",
}


def _modules_calling(attr):
    out = set()
    for p in sorted(ROOT.rglob("*.py")):
        if _calls(_tree(p), attr):
            out.add(str(p.relative_to(ROOT)))
    return out


def test_no_new_service_call_path():
    assert _modules_calling("async_call") == SERVICE_CALLERS


def test_no_new_announcement_path():
    assert _modules_calling("async_announce") == ANNOUNCERS


def test_observer_speaks_only_after_the_output_gate():
    """In _process_event, can_announce is consulted before _speak, and the
    decision comes from reasoning_loop.decide (the coordinator)."""
    tree = _tree(ROOT / "observer.py")
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_event")
    src = ast.unparse(fn)
    assert src.index("reasoning_loop.decide(") < src.index("output_gate.can_announce(")
    assert src.index("output_gate.can_announce(") < src.index("await _speak(")
    assert src.count("await _speak(") == 1


def test_reasoning_loop_decide_delegates_to_the_coordinator():
    tree = _tree(ROOT / "reasoning_loop.py")
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "decide")
    assert "_coordinator.decide(" in ast.unparse(fn)
    assert _calls(tree, "execute_chat") == []


def test_pattern_analysis_is_not_on_the_state_change_hot_path():
    """Scoring and analysis run from the analyzer, never from a
    state-changed listener in the observer or the cognitive core."""
    for mod in ("observer.py", "cognitive_core.py"):
        tree = _tree(ROOT / mod)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and fn.name == "_on_state_changed":
                src = ast.unparse(fn)
                for forbidden in ("analyze", "score_time_routine", "routine_evidence",
                                  "cognitive.patterns", "_find_time_routines"):
                    assert forbidden not in src, (mod, forbidden)
