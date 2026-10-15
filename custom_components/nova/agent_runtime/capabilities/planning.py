"""The agent's own planning tools: follow ups it schedules for itself, and
goals it keeps working on."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

# The whole agent logs as ...nova.agent, the name it always had, so filters
# and levels set for it still apply.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")

_RECENT_PROGRESS = 5


def _action(args: dict) -> str:
    return (args.get("action") or "list").lower()


# ── follow ups ──────────────────────────────────────────────────────────────

async def _exec_schedule_followup(hass: HomeAssistant, args: dict) -> str:
    """Queue a piece of work for the agent's future self."""
    from ... import followups
    made = await hass.async_add_executor_job(lambda: followups.schedule(
        args.get("instruction", ""), args.get("delay_minutes", 5),
        context=args.get("context", "") or ""))
    if "error" in made:
        return f"Couldn't schedule that follow-up: {made['error']}"
    return (f"Follow-up #{made['id']} scheduled for {made['due_ts']}: "
            f"\"{made['instruction']}\". I'll run it then and report back.")


async def _exec_manage_followups(hass: HomeAssistant, args: dict) -> str:
    """List the pending follow ups, or cancel one by id."""
    from ... import followups
    if _action(args) == "cancel":
        fid = args.get("followup_id")
        if fid is None:
            return "Which follow-up? Give me its id (use list first)."
        if await hass.async_add_executor_job(lambda: followups.cancel(int(fid))):
            return f"Follow-up #{fid} cancelled."
        return f"No pending follow-up #{fid} found."
    waiting = await hass.async_add_executor_job(followups.pending)
    if not waiting:
        return "No follow-ups pending."
    return "\n".join(["Pending follow-ups:"] + [
        f"  #{row['id']} due {row['due_ts']}: {row['instruction'][:120]}" for row in waiting])


# ── goals ───────────────────────────────────────────────────────────────────

async def _exec_create_goal(hass: HomeAssistant, args: dict) -> str:
    from ... import goals
    interval = args.get("check_interval_minutes") or goals.DEFAULT_INTERVAL_MIN
    made = await hass.async_add_executor_job(lambda: goals.create(
        args.get("title", ""), args.get("outcome", ""), args.get("steps") or [],
        check_interval_min=interval, deadline_minutes=args.get("deadline_minutes")))
    if "error" in made:
        return f"Couldn't open that goal: {made['error']}"
    plan = "".join(f"\n  {step['n']}. {step['step']}" for step in made.get("steps", []))
    deadline = f" Deadline {made['deadline_ts']}." if made.get("deadline_ts") else ""
    return (f"Goal #{made['id']} opened: {made['title']} — {made['outcome']}."
            f"{deadline}{plan}\nI'll start on it within the minute and keep at it; "
            "you'll hear from me when it's done.")


async def _exec_update_goal(hass: HomeAssistant, args: dict) -> str:
    from ... import goals
    gid = args.get("goal_id")
    if gid is None:
        return "update_goal needs goal_id."
    done = await hass.async_add_executor_job(lambda: goals.update(
        int(gid), step_updates=args.get("step_updates"),
        next_check_minutes=args.get("next_check_minutes"), status=args.get("status"),
        result=args.get("result"), progress_note=args.get("progress_note")))
    if "error" in done:
        return f"Couldn't update goal #{gid}: {done['error']}"
    return f"Goal #{gid} progress recorded."


def _goal_status(goal: dict) -> str:
    lines = [f"Goal #{goal['id']} [{goal['status']}] {goal['title']} — {goal['outcome']}"]
    lines += [f"  [{step['status']}] {step['n']}. {step['step']}" for step in goal["steps"]]
    lines += [f"  {note['t']}: {note['note']}" for note in goal["progress"][-_RECENT_PROGRESS:]]
    if goal.get("last_result"):
        lines.append(f"  Result: {goal['last_result']}")
    return "\n".join(lines)


def _goal_line(goal: dict) -> str:
    finished = sum(1 for step in goal["steps"] if step["status"] == "done")
    return (f"  #{goal['id']} {goal['title']} — steps {finished}/{len(goal['steps'])} "
            f"done, next check {goal['next_check_ts']}")


async def _exec_manage_goals(hass: HomeAssistant, args: dict) -> str:
    """List the active goals, show one's status, or cancel one."""
    from ... import goals
    action = _action(args)
    gid = args.get("goal_id")
    if action == "cancel":
        if gid is None:
            return "Which goal? Give me its id (use list first)."
        if await hass.async_add_executor_job(lambda: goals.cancel(int(gid))):
            return f"Goal #{gid} cancelled."
        return f"No active goal #{gid} found."
    if action == "status":
        if gid is None:
            return "status needs goal_id."
        goal = await hass.async_add_executor_job(lambda: goals.get(int(gid)))
        return _goal_status(goal) if goal else f"No goal #{gid}."
    active = await hass.async_add_executor_job(goals.active)
    if not active:
        return "No active goals."
    return "\n".join(["Active goals:"] + [_goal_line(goal) for goal in active])
