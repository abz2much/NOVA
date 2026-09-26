"""Self-scheduled follow-ups and goals."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_schedule_followup(hass: HomeAssistant, args: dict) -> str:
    """The agent queues work for its future self."""
    from ... import followups
    res = await hass.async_add_executor_job(
        lambda: followups.schedule(
            args.get("instruction", ""),
            args.get("delay_minutes", 5),
            context=args.get("context", "") or ""))
    if "error" in res:
        return f"Couldn't schedule that follow-up: {res['error']}"
    return (f"Follow-up #{res['id']} scheduled for {res['due_ts']}: "
            f"\"{res['instruction']}\". I'll run it then and report back.")


async def _exec_manage_followups(hass: HomeAssistant, args: dict) -> str:
    from ... import followups
    action = (args.get("action") or "list").lower()
    if action == "cancel":
        fid = args.get("followup_id")
        if fid is None:
            return "Which follow-up? Give me its id (use list first)."
        ok = await hass.async_add_executor_job(
            lambda: followups.cancel(int(fid)))
        return (f"Follow-up #{fid} cancelled." if ok
                else f"No pending follow-up #{fid} found.")
    rows = await hass.async_add_executor_job(followups.pending)
    if not rows:
        return "No follow-ups pending."
    lines = ["Pending follow-ups:"]
    for r in rows:
        lines.append(f"  #{r['id']} due {r['due_ts']}: {r['instruction'][:120]}")
    return "\n".join(lines)


async def _exec_create_goal(hass: HomeAssistant, args: dict) -> str:
    from ... import goals
    res = await hass.async_add_executor_job(
        lambda: goals.create(
            args.get("title", ""), args.get("outcome", ""),
            args.get("steps") or [],
            check_interval_min=args.get("check_interval_minutes")
            or goals.DEFAULT_INTERVAL_MIN,
            deadline_minutes=args.get("deadline_minutes")))
    if "error" in res:
        return f"Couldn't open that goal: {res['error']}"
    steps = "".join(f"\n  {s['n']}. {s['step']}" for s in res.get("steps", []))
    dl = f" Deadline {res['deadline_ts']}." if res.get("deadline_ts") else ""
    return (f"Goal #{res['id']} opened: {res['title']} — {res['outcome']}."
            f"{dl}{steps}\nI'll start on it within the minute and keep at it; "
            f"you'll hear from me when it's done.")


async def _exec_update_goal(hass: HomeAssistant, args: dict) -> str:
    from ... import goals
    gid = args.get("goal_id")
    if gid is None:
        return "update_goal needs goal_id."
    res = await hass.async_add_executor_job(
        lambda: goals.update(
            int(gid), step_updates=args.get("step_updates"),
            next_check_minutes=args.get("next_check_minutes"),
            status=args.get("status"), result=args.get("result"),
            progress_note=args.get("progress_note")))
    if "error" in res:
        return f"Couldn't update goal #{gid}: {res['error']}"
    return f"Goal #{gid} progress recorded."


async def _exec_manage_goals(hass: HomeAssistant, args: dict) -> str:
    from ... import goals
    action = (args.get("action") or "list").lower()
    if action == "cancel":
        gid = args.get("goal_id")
        if gid is None:
            return "Which goal? Give me its id (use list first)."
        ok = await hass.async_add_executor_job(lambda: goals.cancel(int(gid)))
        return (f"Goal #{gid} cancelled." if ok
                else f"No active goal #{gid} found.")
    if action == "status":
        gid = args.get("goal_id")
        if gid is None:
            return "status needs goal_id."
        g = await hass.async_add_executor_job(lambda: goals.get(int(gid)))
        if not g:
            return f"No goal #{gid}."
        lines = [f"Goal #{g['id']} [{g['status']}] {g['title']} — {g['outcome']}"]
        for s in g["steps"]:
            lines.append(f"  [{s['status']}] {s['n']}. {s['step']}")
        for p in g["progress"][-5:]:
            lines.append(f"  {p['t']}: {p['note']}")
        if g.get("last_result"):
            lines.append(f"  Result: {g['last_result']}")
        return "\n".join(lines)
    rows = await hass.async_add_executor_job(goals.active)
    if not rows:
        return "No active goals."
    lines = ["Active goals:"]
    for g in rows:
        done = sum(1 for s in g["steps"] if s["status"] == "done")
        lines.append(f"  #{g['id']} {g['title']} — steps {done}/{len(g['steps'])} "
                     f"done, next check {g['next_check_ts']}")
    return "\n".join(lines)
