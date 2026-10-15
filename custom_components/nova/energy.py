"""Whole house energy: how much the home is drawing and what to say about it.

appliance_monitor.py finds the whole home meter and learns appliances by
their wattage. This module reads the live draw against a peak the user sets,
lists which declared appliances are running, and offers advice when the
house is over peak with loads that could be staggered. It also passes on
the energy outlook's advice (energy_outlook.py) through the same tick.

Nothing here switches a load. Appliance profiles hold a power sensor, never
something Nova can control, so every offer is information only at every
agency level. The agency setting (``energy_agency``: advisory, opt_in or
autonomous, optionally raised one step by a mode listed in
``energy_mode_bump``) only changes how the offer is worded. Loads whose name
suggests they are critical (medical, fridge, network, heating and so on) are
never suggested for holding.

No public function raises.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# How far Nova may go, lowest first. Today every level only words the offer.
AGENCY_ADVISORY, AGENCY_OPT_IN, AGENCY_AUTONOMOUS = "advisory", "opt_in", "autonomous"
_AGENCY_ORDER = {AGENCY_ADVISORY: 0, AGENCY_OPT_IN: 1, AGENCY_AUTONOMOUS: 2}

_DEFAULT_PEAK_W = 8000.0
_RUNNING_W = 50.0   # an appliance drawing more than this counts as running

# Words in an entity id or name that mark a load as never to be held.
_NEVER_SHED = ("medical", "cpap", "oxygen", "fridge", "freezer", "refrigerator",
               "security", "alarm", "network", "router", "modem", "server",
               "sump", "well_pump", "furnace", "boiler", "heat")

# Friendly name fragments that identify a whole home meter when the
# appliance monitor found none.
_METER_NAMES = ("electric consumption", "home consumption", "home energy",
                "total consumption", "main power", "whole house",
                "grid consumption", "mains power")

# Set when the over peak offer has been made, cleared by the next reading
# under peak, so one long high draw evening is mentioned once (8.49.37).
_peak_spell_offered = False


def _cfg(key: str, default=None):
    try:
        from . import nova_config
        value = nova_config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _configured_agency() -> str:
    agency = str(_cfg("energy_agency", AGENCY_ADVISORY) or AGENCY_ADVISORY).lower()
    return agency if agency in _AGENCY_ORDER else AGENCY_ADVISORY


def _mode_agency_bonus() -> int:
    """1 while the active mode is listed in ``energy_mode_bump``, else 0. The
    list is empty by default, so modes change nothing unless the user asks."""
    try:
        from . import modes
        bump = _cfg("energy_mode_bump", [])
        if isinstance(bump, list) and modes.active_mode() in bump:
            return 1
    except Exception:
        pass
    return 0


def effective_agency() -> str:
    """The configured agency, one step higher while a bump mode is active,
    never above autonomous."""
    rank = min(_AGENCY_ORDER[_configured_agency()] + _mode_agency_bonus(),
               _AGENCY_ORDER[AGENCY_AUTONOMOUS])
    for name, level in _AGENCY_ORDER.items():
        if level == rank:
            return name
    return _configured_agency()


def _peak_threshold() -> float:
    """``energy_peak_watts``, or 8 kW when it is unset or not a number."""
    setting = _cfg("energy_peak_watts", _DEFAULT_PEAK_W) or _DEFAULT_PEAK_W
    try:
        return float(setting)
    except (ValueError, TypeError):
        return _DEFAULT_PEAK_W


def _never_shed(entity_id: str, name: str) -> bool:
    text = f"{entity_id} {name}".lower()
    return any(word in text for word in _NEVER_SHED)


def _watts(state) -> float:
    """A power reading in watts; raises ValueError or TypeError when the
    state is not a number."""
    value = float(state.state)
    if (state.attributes.get("unit_of_measurement") or "").lower() == "kw":
        value *= 1000.0
    return value


# ── the live picture ─────────────────────────────────────────────────────────

def _read_whole_home_watts(hass) -> tuple[Optional[float], str]:
    """(watts, meter entity). The meter is the one the appliance monitor
    found, else one recognised by name. (None, '') when there is no meter and
    (None, meter) when it exists but gives no number."""
    meter = None
    try:
        from . import appliance_monitor
        found = appliance_monitor._discover_whole_home_meter(hass)
        meter = getattr(found, "entity_id", None) if found else None
    except Exception:
        meter = None
    meter = meter or _fallback_meter(hass)
    if not meter:
        return None, ""
    state = hass.states.get(meter)
    if state is None:
        return None, meter
    try:
        return _watts(state), meter
    except (ValueError, TypeError):
        return None, meter


def _fallback_meter(hass) -> Optional[str]:
    for state in hass.states.async_all("sensor"):
        unit = (state.attributes.get("unit_of_measurement") or "").lower()
        if state.attributes.get("device_class", "") != "power" and unit not in ("w", "kw"):
            continue
        name = (state.attributes.get("friendly_name") or "").lower()
        if any(fragment in name for fragment in _METER_NAMES):
            return state.entity_id
    return None


def _running_appliances(hass) -> list[dict]:
    """Declared appliances (the ``appliances`` setting) drawing power now,
    biggest first. Each is {name, entity, watts, shed_ok}."""
    declared = _cfg("appliances", [])
    if not isinstance(declared, list):
        return []
    running = []
    for appliance in declared:
        if not isinstance(appliance, dict):
            continue
        name = str(appliance.get("name", "") or "")
        eid = str(appliance.get("entity", "") or "")
        state = hass.states.get(eid) if eid else None
        if state is None:
            continue
        try:
            watts = _watts(state)
        except (ValueError, TypeError):
            continue
        if watts > _RUNNING_W:
            running.append({"name": name, "entity": eid, "watts": round(watts),
                            "shed_ok": not _never_shed(eid, name)})
    running.sort(key=lambda item: -item["watts"])
    return running


def _advice_line(watts: Optional[float], meter: str, peak: float, running: list) -> str:
    kw, peak_kw = (watts or 0.0) / 1000, peak / 1000
    if watts is None and meter:
        return ("The whole home power meter isn't reporting right now, so the "
                "current draw is unknown.")
    if watts is None:
        return ("No whole-home power meter found — set one up (an Electric "
                "Consumption [W] sensor) to enable energy management.")
    if watts < peak:
        return f"Drawing {kw:.1f} kW — within the {peak_kw:.1f} kW comfort range."
    movable = [item for item in running if item["shed_ok"]]
    if len(movable) < 2:
        return f"Drawing {kw:.1f} kW, above the {peak_kw:.1f} kW peak."
    names = " + ".join(item["name"] for item in movable[:3] if item["name"])
    return (f"Drawing {kw:.1f} kW (over the {peak_kw:.1f} kW peak) — "
            f"{names or 'multiple loads'} running at once. Staggering them "
            f"would cut the peak.")


def power_status(hass) -> dict:
    """The energy picture for the panel and the agent."""
    watts, meter = _read_whole_home_watts(hass)
    peak = _peak_threshold()
    running = _running_appliances(hass)
    return {
        "watts": None if watts is None else round(watts),
        "kw": None if watts is None else round(watts / 1000, 2),
        "meter": meter,
        "peak_watts": round(peak),
        "over_peak": watts is not None and watts >= peak,
        "agency": effective_agency(),
        "configured_agency": _configured_agency(),
        "running": running,
        "advice": [_advice_line(watts, meter, peak, running)],
    }


def _peak_offer(agency: str, kw: float, names: str, held: dict) -> dict:
    """The over peak offer in the wording for ``agency``. None of them acts:
    auto_act is always False (see the module note)."""
    if agency == AGENCY_AUTONOMOUS:
        return {"type": "energy_shed", "urgency": "low", "auto_act": False,
                "entity_id": held["entity"],
                "message": f"{names} are both running at {kw:.1f} kW, over your "
                           f"peak. I'd normally ease that by holding "
                           f"{held['name']}, but I don't have a way to actually "
                           f"control it yet.",
                "energy": True}
    if agency == AGENCY_OPT_IN:
        return {"type": "energy_shed_offer", "urgency": "low", "auto_act": False,
                "entity_id": held["entity"],
                "message": f"{names} are drawing {kw:.1f} kW together, over your "
                           f"peak. Holding {held['name']} until the load drops "
                           f"would help.",
                "energy": True}
    return {"type": "energy_advice", "urgency": "low", "auto_act": False,
            "message": f"Heads up — {names} are running at {kw:.1f} kW, over your "
                       f"peak. Staggering them would help.",
            "energy": True}


def evaluate_for_proactive(hass) -> Optional[dict]:
    """The proactive tick's over peak offer, or None. One offer per over
    peak spell, and only when at least two loads that may be held are running
    (one big load cannot be staggered)."""
    global _peak_spell_offered
    try:
        status = power_status(hass)
    except Exception:
        return None
    if not status.get("over_peak"):
        if status.get("watts") is not None:
            _peak_spell_offered = False
        return None
    if _peak_spell_offered:
        return None
    movable = [item for item in status["running"] if item["shed_ok"]]
    if len(movable) < 2:
        return None
    names = " and ".join(item["name"] for item in movable[:2] if item["name"])
    _peak_spell_offered = True
    return _peak_offer(status["agency"], status["kw"],
                       names or "two high-draw loads", movable[0])


# ── energy outlook advice (8.12.0) ───────────────────────────────────────────
# Offered through the same tick. Each advice key already holds its kind and
# the local date; the time it was last offered is kept in memory only.
OUTLOOK_COOLDOWN_S = 6 * 3600
_outlook_offered: dict[str, float] = {}
_ONCE_A_DAY = ("cheap_topup",)   # offered once per key, not every six hours
_OUTLOOK_KINDS = ("battery_hold", "cheap_topup", "high_use_today")


def _outlook_due(key: str, kind: str, now: float) -> bool:
    last = _outlook_offered.get(key)
    if last is None:
        return True
    return kind not in _ONCE_A_DAY and now - last >= OUTLOOK_COOLDOWN_S


def _forget_old_outlook_keys(now: float) -> None:
    for key in [k for k, at in _outlook_offered.items() if now - at > 2 * 86400]:
        _outlook_offered.pop(key, None)


async def evaluate_outlook_for_proactive(hass) -> Optional[dict]:
    """One piece of outlook advice, or None.

    Advice only at every agency level: auto_act is False and there is no
    action_data. The caller applies the kill switch and mode gate as for the
    over peak offer, and delivery handles quiet hours and urgency. The output
    gate (mutes, rate limit, duplicates) is checked here, because that
    delivery path does not check it. An offer the gate stops still uses its
    cooldown, so it is not retried every tick."""
    try:
        from . import energy_outlook
        outlook = await energy_outlook.energy_outlook_status(hass)
        if outlook.get("error") or not outlook.get("configured"):
            return None
        now = time.time()
        _forget_old_outlook_keys(now)
        for advice in outlook.get("advice") or []:
            kind, key = advice.get("kind"), advice.get("key")
            if kind not in _OUTLOOK_KINDS or not key or not _outlook_due(key, kind, now):
                continue
            _outlook_offered[key] = now
            return _gated_outlook_offer(hass, advice.get("message") or "")
    except Exception as exc:
        _LOGGER.debug("energy outlook offer failed: %s", exc)
    return None


def _gated_outlook_offer(hass, message: str) -> Optional[dict]:
    from . import output_gate
    allowed, reason = output_gate.can_announce(
        entity_id="energy_outlook", category="energy", urgency="low", message=message)
    output_gate.record_announcement(
        entity_id="energy_outlook", category="energy", urgency="low",
        message=message, was_spoken=allowed)
    if not allowed:
        _LOGGER.debug("energy outlook offer suppressed: %s", reason)
        return None
    try:
        from .core_common import _live_honorific, _persona
        message = _persona().lead_in(_live_honorific(hass), message[:1].lower() + message[1:])
    except Exception:
        pass
    return {"type": "energy_outlook_advice", "urgency": "low", "auto_act": False,
            "message": message, "energy": True}
