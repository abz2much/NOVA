"""The household reading and the shared safety record for Nova's alerts.

8.22.0 gathered every alert source's decisions here. Since 8.45.0 every
proactive message is decided and delivered by delivery.py (design D3), and
this module keeps what the sources still share:

* situation()  reads the household once: residents and alarm posture from
               household.py, plus whether the house is asleep and whether it
               is quiet hours.
* nobody_home  the wording rule: "no one is home" is only ever said when the
               residents are known to be away.
* Plan         what was done with one alert, for the record.
* record_decision  writes a safety decision to the Decision Record in one
               shared format (8.23.0).

Intrusion decisions stay in core_safety.py; their actions reach the speakers
and phones through delivery.py, like every other core action.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# ── the situation ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Situation:
    """One reading of the house for one alert."""
    house: object = None          # household.Household, or None if unreadable
    sleeping: bool = False
    quiet: bool = False
    # The facts this reading used, each with its source and when it was read
    # (world.Fact.record, 8.46.0). Kept with the decision; never compared.
    read: dict = field(default_factory=dict, compare=False)
    # Names the reading (world.World.id): the tick's own while a tick runs,
    # otherwise one taken for this decision (8.47.0).
    snapshot_id: str = field(default="", compare=False)

    @property
    def residents(self) -> str:
        from . import household
        return getattr(self.house, "residents", household.UNKNOWN)

    @property
    def anyone_home(self) -> bool:
        return bool(getattr(self.house, "anyone_home", False))


def situation(hass, config: Optional[dict] = None, *, sleeping=None,
              quiet=None, house=None) -> Situation:
    """Read the household (household.py) with sleep and quiet hours. Each
    comes from the world.py resolvers unless the caller passes its own
    reading (a world.Fact, or a plain True or False). While a 30 second
    tick runs, the household comes from that tick's snapshot (8.24.0).
    Never raises."""
    from . import world
    if house is None:
        try:
            from . import household
            house = household.snapshot(hass, config)
        except Exception as exc:
            _LOGGER.debug("alert_path: household reading failed: %s", exc)
            house = None
    facts = {}
    current = world.current()
    snapshot_id = current.id if current is not None and current.id else world.new_id()
    if house is not None:
        facts["household"] = world.Fact(house, getattr(house, "source", ""),
                                        getattr(house, "read_at", 0.0)).record()
    else:
        facts["household"] = world.Fact(None, "unreadable").record()
    values = {}
    for name, given, resolve in (("asleep", sleeping, world.asleep),
                                 ("quiet_hours", quiet, world.quiet_hours)):
        if given is None and current is not None and getattr(current, name) != world.UNKNOWN:
            # Inside a tick: the tick's own reading, so the tick agrees.
            given = world.Fact(getattr(current, name), current.sources.get(name, ""),
                               current.read_at)
        if given is None:
            given = resolve(hass)
        elif not isinstance(given, world.Fact):
            given = world.Fact(bool(given), "the decision's own reading")
        facts[name] = given.record()
        values[name] = given.value is True
    return Situation(house=house, sleeping=values["asleep"], quiet=values["quiet_hours"],
                     read=facts, snapshot_id=snapshot_id)


def nobody_home(sit: Situation) -> bool:
    """The wording rule: say "no one is home" only when the residents are
    known to be away. Unknown is never away."""
    from . import household
    return sit.residents == household.AWAY


# ── the plan ────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    """What was done with one alert, for the Decision Record."""
    alert: bool = True                # False: nothing is spoken or pushed
    speak: bool = False
    targets: list = field(default_factory=list)
    push: bool = False
    mode: str = ""                    # the speaker routing mode, for the record
    reason: str = ""


# ── the decision record (8.23.0) ────────────────────────────────────────────
# Every safety decision (intrusion, lockdown, the nighttime sweep, hazards,
# packages and door alerts) and every Observer alert decision (8.47.0) is
# written to the Decision Record in one format:
#   observation     source, entity, message, residents, alarm posture,
#                   asleep, quiet hours, plus the decision's own facts,
#                   and "snapshot_id", the reading it was taken from
#   interpretation  {"assessment": what Nova concluded}
#   evidence        {"spoken", "pushed", "route"} from the delivery plan,
#                   "delivery": what delivery decided, and "request_ids":
#                   the Actions log requests it caused
#   decision        what was done, in words
#   reason          why
# Records are written with no outcome, so they never change the interruption
# budget, calibration or adaptive awareness, which read judged records only.

def _plan_words(plan: Optional[Plan]) -> str:
    if plan is None:
        return ""
    if not plan.alert:
        return "no alert"
    return {(True, True): "spoken and pushed to phones", (True, False): "spoken",
            (False, True): "pushed to phones"}.get((plan.speak, plan.push), "not delivered")


def decision_fields(kind: str, *, source: str, sit: Optional[Situation] = None,
                    plan: Optional[Plan] = None, entity_id: Optional[str] = None,
                    message: str = "", facts: Optional[dict] = None,
                    assessment: str = "", decision: str = "", reason: str = "",
                    ref: Optional[str] = None,
                    request_ids: Optional[list] = None,
                    delivery: str = "") -> dict:
    """The shared Decision Record fields for one safety decision."""
    observation: dict = {"source": source}
    if entity_id:
        observation["entity_id"] = entity_id
    if message:
        observation["message"] = str(message)[:300]
    if sit is not None:
        observation.update({
            "residents": sit.residents,
            "posture": getattr(sit.house, "posture", None),
            "asleep": sit.sleeping,
            "quiet_hours": sit.quiet,
        })
        if sit.read:
            # Where each of those came from and when it was read (8.46.0).
            observation["read"] = dict(sit.read)
        if sit.snapshot_id:
            observation["snapshot_id"] = sit.snapshot_id
    observation.update(facts or {})
    evidence: dict = {}
    if plan is not None:
        evidence = {"spoken": bool(plan.alert and plan.speak),
                    "pushed": bool(plan.alert and plan.push),
                    "route": plan.mode or None}
    if delivery:
        evidence["delivery"] = str(delivery)
    if request_ids:
        # The Actions log requests this decision caused (8.25.0), so a
        # decision can be traced to its commands and their checks.
        evidence["request_ids"] = [str(r) for r in request_ids if r]
    return {
        "kind": kind,
        "observation": observation,
        "interpretation": {"assessment": assessment} if assessment else {},
        "evidence": evidence,
        "decision": decision or _plan_words(plan),
        "reason": reason or (plan.reason if plan is not None else ""),
        # Links the entries of one episode, e.g. a lockdown and the checks of
        # what it secured (8.24.0). Records themselves are never edited.
        "ref": ref,
    }


def record_decision(kind: str, **fields) -> Optional[int]:
    """Write one safety decision. Returns the record id, or None. Never
    raises: a logging failure must never affect the decision."""
    try:
        from . import decision_record
        return decision_record.record(**decision_fields(kind, **fields))
    except Exception as exc:
        _LOGGER.debug("alert_path: decision record failed: %s", exc)
        return None


async def async_record_decision(hass, kind: str, **fields) -> Optional[int]:
    """record_decision off the event loop. Never raises."""
    try:
        return await hass.async_add_executor_job(
            lambda: record_decision(kind, **fields))
    except Exception as exc:
        _LOGGER.debug("alert_path: decision record failed: %s", exc)
        return None
