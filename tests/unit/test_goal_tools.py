"""The agent's goal tools (create_goal, update_goal, manage_goals): that they
are offered to the model, reach their executors, and turn the goal store's
answers into the replies the model reads. The store itself has its own
tests in test_goals.py; here every call goes through the agent façade."""
from datetime import datetime

import pytest

CLOCK = datetime(2026, 7, 12, 9, 0, 0)
STAMP = "2026-07-12 09:00:00"

GOAL_TOOLS = {
    "create_goal": ("_exec_create_goal", ["title", "outcome"]),
    "update_goal": ("_exec_update_goal", ["goal_id"]),
    "manage_goals": ("_exec_manage_goals", ["action"]),
}


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def store(load, tmp_path, monkeypatch):
    """The goals module on a private database with its clock pinned, so the
    timestamps in replies are known in advance."""
    goals = load("goals")
    monkeypatch.setattr(goals, "DB_PATH", str(tmp_path / "goal_tools.db"))
    monkeypatch.setattr(goals, "_now", lambda now=None: now or CLOCK)
    return goals


@pytest.fixture
def call(agent, store, fake_hass):
    """call(tool, **args) awaits the named executor as the dispatcher would."""
    async def _invoke(tool, **args):
        executor = getattr(agent, GOAL_TOOLS[tool][0])
        return await executor(fake_hass, args)
    return _invoke


def _spec(agent, name):
    found = [t["function"] for t in agent.NOVA_TOOLS if t["function"]["name"] == name]
    assert len(found) == 1, f"{name} should be declared exactly once"
    return found[0]


# ── what the model is offered ───────────────────────────────────────────────

@pytest.mark.parametrize("tool", sorted(GOAL_TOOLS))
def test_each_goal_tool_is_declared_and_mapped_to_its_executor(agent, tool):
    executor_name, required = GOAL_TOOLS[tool]
    spec = _spec(agent, tool)
    assert spec["parameters"]["required"] == required
    assert tool in agent._TOOL_MAP
    assert agent._TOOL_MAP[tool] is getattr(agent, executor_name)


# ── the whole lifecycle through the tools ───────────────────────────────────

async def test_open_progress_inspect_and_cancel_a_goal(call, store):
    opened = await call("create_goal", title="Warm den", outcome="den at 72F",
                        steps=["set thermostat", "confirm temperature"])
    assert opened.startswith("Goal #1 opened: Warm den — den at 72F.")
    assert "\n  1. set thermostat\n  2. confirm temperature\n" in opened
    assert "Deadline" not in opened

    listing = await call("manage_goals", action="list")
    assert listing == ("Active goals:\n"
                       f"  #1 Warm den — steps 0/2 done, next check {STAMP}")

    recorded = await call("update_goal", goal_id=1,
                          step_updates=[{"n": 1, "status": "done"}],
                          progress_note="thermostat set")
    assert recorded == "Goal #1 progress recorded."

    status = await call("manage_goals", action="status", goal_id=1)
    assert status.splitlines() == [
        "Goal #1 [active] Warm den — den at 72F",
        "  [done] 1. set thermostat",
        "  [pending] 2. confirm temperature",
        f"  {STAMP}: thermostat set",
    ]

    assert await call("manage_goals", action="cancel", goal_id=1) == "Goal #1 cancelled."
    assert store.get(1)["status"] == "cancelled"
    assert await call("manage_goals", action="list") == "No active goals."


async def test_a_deadline_and_interval_reach_the_store(call, store):
    opened = await call("create_goal", title="Guest prep", outcome="house ready",
                        steps=[], check_interval_minutes=45, deadline_minutes=90)
    assert " Deadline 2026-07-12 10:30:00." in opened
    saved = store.get(1)
    assert saved["check_interval_min"] == 45
    assert saved["deadline_ts"] == "2026-07-12 10:30:00"


async def test_missing_interval_uses_the_store_default(call, store):
    await call("create_goal", title="Air out", outcome="windows closed by dusk")
    assert store.get(1)["check_interval_min"] == store.DEFAULT_INTERVAL_MIN


async def test_a_closing_update_shows_its_result_in_status(call):
    await call("create_goal", title="Tidy", outcome="kitchen tidy", steps=["wipe"])
    await call("update_goal", goal_id=1, status="done", result="All clean.")
    status = await call("manage_goals", action="status", goal_id=1)
    assert status.splitlines()[0] == "Goal #1 [done] Tidy — kitchen tidy"
    assert status.splitlines()[-1] == "  Result: All clean."
    # A closed goal is no longer active, so it cannot be cancelled.
    assert await call("manage_goals", action="cancel", goal_id=1) == "No active goal #1 found."


async def test_status_shows_only_the_latest_five_notes(call):
    await call("create_goal", title="Watch", outcome="leak sensor stays dry")
    for n in range(7):
        await call("update_goal", goal_id=1, progress_note=f"check {n}")
    notes = [line for line in (await call("manage_goals", action="status", goal_id=1))
             .splitlines() if ": check " in line]
    assert [line.rsplit(" ", 1)[1] for line in notes] == ["2", "3", "4", "5", "6"]


# ── replies when something is missing or refused ────────────────────────────

@pytest.mark.parametrize("tool, args, expected", [
    ("create_goal", {"title": "Nothing", "outcome": "  "},
     "Couldn't open that goal: empty outcome"),
    ("update_goal", {"progress_note": "no id"}, "update_goal needs goal_id."),
    ("update_goal", {"goal_id": 9, "progress_note": "x"},
     "Couldn't update goal #9: no goal #9"),
    ("manage_goals", {"action": "cancel"}, "Which goal? Give me its id (use list first)."),
    ("manage_goals", {"action": "cancel", "goal_id": 4}, "No active goal #4 found."),
    ("manage_goals", {"action": "status"}, "status needs goal_id."),
    ("manage_goals", {"action": "status", "goal_id": 4}, "No goal #4."),
    ("manage_goals", {}, "No active goals."),
], ids=["empty-outcome", "update-no-id", "update-unknown", "cancel-no-id",
        "cancel-unknown", "status-no-id", "status-unknown", "list-empty"])
async def test_refusals_are_plain_replies(call, tool, args, expected):
    assert await call(tool, **args) == expected


async def test_action_names_are_case_insensitive(call):
    await call("create_goal", title="Lights", outcome="porch light on at dusk")
    assert (await call("manage_goals", action="LIST")).startswith("Active goals:\n  #1 Lights")
    assert await call("manage_goals", action="Cancel", goal_id=1) == "Goal #1 cancelled."


async def test_a_full_goal_list_is_reported_not_raised(call, store, monkeypatch):
    monkeypatch.setattr(store, "MAX_ACTIVE", 1)
    await call("create_goal", title="One", outcome="first outcome")
    refused = await call("create_goal", title="Two", outcome="second outcome")
    assert refused == "Couldn't open that goal: goal list is full (1 active)"
    assert [g["title"] for g in store.active()] == ["One"]
