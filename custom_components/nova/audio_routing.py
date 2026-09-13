"""
Nova — Audio routing (v7.92.0, explicit per-room speaker assignment).

HA's area registry still resolves WHICH area a satellite/event belongs to.
But WHICH speaker plays there is no longer auto-discovered — that used to
mean "any media_player HA happens to place in this area, minus ones tagged
device_class 'tv'", a denylist that a stray, untagged duplicate entity can
slip through (a real bug: Music Assistant/AirPlay/Cast integrations each
register their own separate media_player for the same physical TV, and a
user tagging most of them 'tv' can still miss one — see room_speaker()'s
docstring). Room speakers are now an explicit allowlist: Nova only ever
speaks through the ONE entity assigned to a room, plus one general
fallback speaker for rooms with nothing assigned.

What Nova recognizes per area:
  - SATELLITES:  entities with domain 'assist_satellite' (ears, never output)
  - PRESENCE:    entities with domain 'binary_sensor', device_class 'occupancy'
                 (mmWave, motion, or any occupancy detector)

The user's routing config:
  - bedroom_areas:    list of HA area_ids flagged for sleep detection
  - room_speakers:    {area_id: media_player_entity_id} explicit assignment
  - general_speaker:  single media_player entity, room-targeting fallback
                       only (does not affect broadcast_group below)
  - broadcast_group:  single media_player entity for critical/high urgency
                       broadcasts (typically the "home" Cast group)

Routing rules:

  REPLY (someone spoke to a satellite):
    1. Get the satellite's area.
    2. That area's assigned room speaker, if any → speak there.
    3. Else the general speaker, if set → speak there.
    4. Else speak through the satellite itself (its own built-in speaker).
       Per Username's directive: "if the satellite is the only thing in
       earshot, it wins."

  OBSERVER (proactive announcement):
    Urgency CRITICAL (smoke/leak/door forced):
      → broadcast_group, overrides sleep state, always
    Urgency HIGH (doorbell, security event):
      → broadcast_group if someone is home; else mobile notification
    Urgency MEDIUM:
      → the room speaker assigned to the room where presence is detected,
         else the general speaker;
         fallback to broadcast_group if no presence detected and someone is home;
         fallback to mobile notification if nobody home
    Urgency LOW:
      → the room speaker assigned to the room where presence is detected,
         else the general speaker;
         silent (queue) if no local presence — don't interrupt from another room

  BROADCAST (briefing, sentinel, doorbell — explicit group announcement):
      → broadcast_group / announcement_speakers, unaffected by room_speakers
         or general_speaker; silent until the user configures one.

Routing always EXCLUDES voice satellites from being output targets, except
the explicit "satellite fallback" case in REPLY above.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

_LOGGER = logging.getLogger(__name__)


# ─── Entity / area resolution helpers ────────────────────────────────────────

def entity_area(hass: HomeAssistant, entity_id: str) -> Optional[str]:
    """Return the HA area_id for an entity (direct, or via its device).
    Returns None if no area is assigned."""
    try:
        ent_reg = er.async_get(hass)
        ent = ent_reg.async_get(entity_id)
        if ent is None:
            return None
        if ent.area_id:
            return ent.area_id
        if ent.device_id:
            dev_reg = dr.async_get(hass)
            dev = dev_reg.async_get(ent.device_id)
            if dev and dev.area_id:
                return dev.area_id
    except Exception as exc:
        _LOGGER.debug("area lookup failed for %s: %s", entity_id, exc)
    return None


def _entities_by_domain(
    hass: HomeAssistant, domain: str
) -> list[str]:
    """Return all entity_ids for a given domain."""
    return [s.entity_id for s in hass.states.async_all(domain)]


def _entities_by_domain_and_device_class(
    hass: HomeAssistant, domain: str, device_class: str
) -> list[str]:
    """Return all entity_ids for (domain, device_class)."""
    out = []
    for s in hass.states.async_all(domain):
        dc = s.attributes.get("device_class")
        if dc == device_class:
            out.append(s.entity_id)
    return out


# ─── Per-area discovery ──────────────────────────────────────────────────────

def satellites_in_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """All assist_satellite entities whose area matches."""
    return [
        e for e in _entities_by_domain(hass, "assist_satellite")
        if entity_area(hass, e) == area_id
    ]


def room_speaker(hass: HomeAssistant, area_id: str) -> Optional[str]:
    """The one speaker explicitly assigned to this room (v7.92.0), or None.

    Replaces the old approach of auto-discovering every media_player HA
    happens to place in an area and excluding TVs by device_class — a
    denylist that a stray, untagged duplicate entity can slip through (a
    real bug: Music Assistant/AirPlay/Cast each register their own
    media_player for the same physical TV, and a user tagging most of them
    'tv' can still miss one). An explicit per-room assignment is immune to
    however many duplicate entities an integration creates, since nothing
    is ever auto-discovered — Nova only ever considers the one entity
    assigned here.
    """
    try:
        from . import nova_config
        assigned = (nova_config.get("room_speakers", {}) or {}).get(area_id)
    except Exception:
        assigned = None
    if assigned and hass.states.get(assigned) is not None:
        return assigned
    return None


def general_speaker_target(hass: HomeAssistant) -> Optional[str]:
    """The one general-purpose fallback speaker (v7.92.0), used when Nova
    needs to speak in a room that has no speaker explicitly assigned. Does
    NOT affect whole-house broadcasts (briefing/sentinel/doorbell), which
    already use the separate, already-explicit broadcast_group/
    announcement_speakers settings."""
    try:
        from . import nova_config
        eid = nova_config.get("general_speaker", "") or ""
    except Exception:
        eid = ""
    if eid and hass.states.get(eid) is not None:
        return eid
    return None


def _is_display_target(hass: HomeAssistant, entity_id: str, movie: str) -> bool:
    """True if this media_player is a screen (a TV or the designated movie
    player) and must never be a TTS/announcement target."""
    if not entity_id:
        return False
    if movie and entity_id == movie:
        return True
    st = hass.states.get(entity_id)
    if st is not None and st.attributes.get("device_class") == "tv":
        return True
    return False


def drop_display_targets(hass: HomeAssistant, targets, context: str = "") -> list[str]:
    """Final safety choke point: strip TVs and the designated movie player from a
    TTS target list, no matter how those targets were resolved. Every speech path
    (async_announce, the observer's raw tts.speak, proactive audio) runs its
    targets through here so a television can never be spoken to — even via a
    routing path we haven't accounted for.

    Any drop is logged at WARNING with the caller context, so an unexpected TV
    target is immediately traceable to the feature that produced it.
    """
    # Read the designated movie player from the in-memory runtime_config (seeded
    # from config.json at setup) — NOT via nova_config.get(), whose lazy load()
    # mutates shared module state and would perturb unrelated code/tests.
    movie = ""
    try:
        from .const import DOMAIN
        for _ed in (hass.data.get(DOMAIN) or {}).values():
            if isinstance(_ed, dict):
                rc = _ed.get("runtime_config") or {}
                mv = rc.get("movie_media_player")
                if mv:
                    movie = mv
                    break
    except Exception:
        movie = ""
    kept, dropped = [], []
    for t in list(targets or []):
        (dropped if _is_display_target(hass, t, movie) else kept).append(t)
    if dropped:
        _LOGGER.warning(
            "Nova: refused to send TTS to display target(s) %s (context=%s) — "
            "a routing path tried to speak through a TV; speaking to %s instead",
            dropped, context or "?", kept or "nothing",
        )
    return kept


def presence_entities_in_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """All binary_sensor.*_occupancy entities (device_class='occupancy' or
    'motion' or 'presence') in the given area."""
    occupancy_classes = ("occupancy", "motion", "presence")
    all_bs = _entities_by_domain(hass, "binary_sensor")
    try:
        from .entity_filter import is_excluded as _excl
    except Exception:
        _excl = lambda _h, _e: False
    out = []
    for e in all_bs:
        if _excl(hass, e):
            continue
        state = hass.states.get(e)
        if state is None:
            continue
        dc = state.attributes.get("device_class")
        if dc not in occupancy_classes:
            continue
        if entity_area(hass, e) == area_id:
            out.append(e)
    return out


def all_areas_with_satellite(hass: HomeAssistant) -> set[str]:
    """Areas that have at least one satellite. Used to iterate over
    'rooms Nova can hear from'."""
    out = set()
    for e in _entities_by_domain(hass, "assist_satellite"):
        area = entity_area(hass, e)
        if area:
            out.add(area)
    return out


def all_areas_with_presence(hass: HomeAssistant) -> set[str]:
    """Areas that have at least one occupancy/motion/presence sensor."""
    occupancy_classes = ("occupancy", "motion", "presence")
    try:
        from .entity_filter import is_excluded as _excl
    except Exception:
        _excl = lambda _h, _e: False
    out = set()
    for s in hass.states.async_all("binary_sensor"):
        if _excl(hass, s.entity_id):
            continue
        dc = s.attributes.get("device_class")
        if dc not in occupancy_classes:
            continue
        area = entity_area(hass, s.entity_id)
        if area:
            out.add(area)
    return out


# ─── Presence / occupancy state ──────────────────────────────────────────────

def is_area_occupied(hass: HomeAssistant, area_id: str) -> bool:
    """Any presence sensor in this area currently reporting 'on'?"""
    for e in presence_entities_in_area(hass, area_id):
        state = hass.states.get(e)
        if state is None:
            continue
        if str(state.state).lower() in ("on", "home", "detected", "true", "occupied"):
            return True
    return False


def currently_occupied_areas(hass: HomeAssistant) -> list[str]:
    """All areas with at least one 'on' presence sensor right now."""
    return [
        area for area in all_areas_with_presence(hass)
        if is_area_occupied(hass, area)
    ]


def anyone_home(hass: HomeAssistant) -> bool:
    """True if anyone appears to be home: any occupancy/motion/presence sensor on
    ANYWHERE (area assignment is not required — a motion sensor with no area still
    proves presence), or any person/device_tracker reading 'home'. Guides the
    'while no one is home' announcement clause and medium/high fallback routing."""
    occ_classes = ("occupancy", "motion", "presence")
    on_states = ("on", "home", "detected", "true", "occupied")
    for s in hass.states.async_all("binary_sensor"):
        if (s.attributes.get("device_class") in occ_classes
                and str(s.state).lower() in on_states):
            return True
    for s in hass.states.async_all("person"):
        if str(s.state).lower() == "home":
            return True
    for s in hass.states.async_all("device_tracker"):
        if str(s.state).lower() == "home":
            return True
    return False


# ─── Routing: reply (direct speech) ──────────────────────────────────────────

def reply_target(
    hass: HomeAssistant,
    *,
    satellite_entity_id: Optional[str] = None,
    device_id: Optional[str] = None,
    satellite_pairings: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """
    Pick ONE speaker for a direct reply.

    Priority (v7.92.0 — replaced area auto-discovery with explicit
    assignment; see room_speaker()'s docstring for why):
      0. Explicit satellite_pairings override from panel Settings (if set).
      1. The room speaker explicitly assigned to the satellite's area.
      2. The general fallback speaker.
      3. The satellite itself (its built-in speaker) if nothing else available.
      4. None — caller should handle silence.

    Takes either a satellite entity_id (preferred) or device_id.
    satellite_pairings is {satellite_entity_id: media_player_entity_id}.
    """
    sat_entity = satellite_entity_id
    sat_area = None

    if satellite_entity_id:
        sat_area = entity_area(hass, satellite_entity_id)
    elif device_id:
        try:
            dev_reg = dr.async_get(hass)
            dev = dev_reg.async_get(device_id)
            if dev:
                sat_area = dev.area_id
            # Resolve the satellite entity_id from device_id for pairing lookup
            ent_reg = er.async_get(hass)
            for ent in ent_reg.entities.values():
                if ent.device_id == device_id and ent.domain == "assist_satellite":
                    sat_entity = ent.entity_id
                    break
        except Exception as exc:
            _LOGGER.debug("device_id->area lookup failed: %s", exc)

    # ── Priority 0: explicit panel pairing ────────────────────────────────
    _LOGGER.debug(
        "reply_target ENTRY: sat_entity=%s, device_id=%s, sat_area=%s, "
        "pairings=%s",
        sat_entity, device_id, sat_area,
        list(satellite_pairings.keys()) if satellite_pairings else None,
    )
    if satellite_pairings and sat_entity and sat_entity in satellite_pairings:
        paired = satellite_pairings[sat_entity]
        if paired and hass.states.get(paired):
            _LOGGER.debug(
                "reply_target: using panel pairing %s → %s",
                sat_entity, paired,
            )
            return paired
        _LOGGER.debug(
            "reply_target: panel pairing %s → %s but entity unavailable, "
            "falling through",
            sat_entity, paired,
        )

    if not sat_area:
        _LOGGER.debug("reply_target: could not resolve satellite area")
        return satellite_entity_id  # fall back: speak through satellite itself

    assigned = room_speaker(hass, sat_area)
    if assigned:
        _LOGGER.debug("reply_target: using room speaker %s for area '%s'", assigned, sat_area)
        return assigned

    general = general_speaker_target(hass)
    if general:
        _LOGGER.debug(
            "reply_target: no room speaker for area '%s', using general speaker %s",
            sat_area, general,
        )
        return general

    _LOGGER.debug(
        "reply_target: no room or general speaker configured for area '%s', "
        "replying through satellite",
        sat_area,
    )
    return satellite_entity_id


def reply_targets(
    hass: HomeAssistant,
    *,
    device_id: Optional[str] = None,
    voice_satellites: Optional[list] = None,       # deprecated, ignored
    reply_speakers: Optional[list] = None,          # deprecated, ignored
    broadcast_speakers: Optional[list] = None,      # deprecated, ignored
    legacy_cast_speakers: Optional[list] = None,    # deprecated, ignored
    room_routing: bool = True,                       # deprecated, always on in v5.3
    satellite_pairings: Optional[dict[str, str]] = None,
) -> list[str]:
    """
    v5.2 backward-compatibility shim — callers passed flat entity lists.

    v5.3 ignores the flat lists and uses HA's area registry. Returns a
    single-item list for consistency with the old signature.

    If device_id is provided, looks up the satellite/speaker in the device's
    area. Otherwise falls back to broadcast targets.
    """
    target = reply_target(
        hass, device_id=device_id, satellite_pairings=satellite_pairings,
    ) if device_id else None
    return [target] if target else broadcast_target(hass)


# ─── Routing: observer mode (proactive) ──────────────────────────────────────

def observer_speak_target(
    hass: HomeAssistant,
    *,
    urgency: str,
    broadcast_group: Optional[str] = None,
    announcement_speakers: Optional[list[str]] = None,
    is_sleeping: bool = False,
) -> tuple[list[str], str]:
    """
    Decide where to speak for an observer-mode announcement.

    Returns (targets, mode) where:
      - targets is a list of media_player entity_ids (may be empty)
      - mode is one of: "local", "broadcast", "notify_only", "suppressed"

    Caller uses `mode` to decide whether to also send a phone notification
    ("notify_only" means audio is suppressed, only the notification fires).

    announcement_speakers: explicit list from the panel Settings toggle.
    When set and non-empty, this overrides broadcast_group for broadcast-mode
    announcements (but NOT for local/room routing).

    Rules:
      CRITICAL: always broadcast (announcement_speakers > broadcast_group > all speakers); overrides sleep
      HIGH:     sleeping → notify only; awake+home → broadcast; away → notify
      MEDIUM:   sleeping → suppressed; present in room → room speaker;
                home but no room presence → broadcast; away → notify
      LOW:      sleeping → suppressed; present in room → room speaker;
                otherwise suppressed (queued — don't interrupt from elsewhere)
    """

    def _broadcast_speakers() -> list[str]:
        """Resolve broadcast speakers: panel toggles > broadcast_group > all."""
        if announcement_speakers:
            # Validate they still exist in HA
            valid = [s for s in announcement_speakers if hass.states.get(s)]
            if valid:
                return valid
        if broadcast_group:
            if hass.states.get(broadcast_group):
                return [broadcast_group]
        # Silent until configured — never fall back to every speaker in the house
        # (v7.83.0). A fresh install with nothing selected stays quiet rather
        # than broadcasting to all devices, including TVs.
        return []

    # ─── CRITICAL ────────────────────────────────────────────────────────────
    if urgency == "critical":
        speakers = _broadcast_speakers()
        return (speakers, "broadcast") if speakers else ([], "notify_only")

    home = anyone_home(hass)

    # ─── HIGH ────────────────────────────────────────────────────────────────
    if urgency == "high":
        if is_sleeping or not home:
            return ([], "notify_only")
        speakers = _broadcast_speakers()
        if speakers:
            return (speakers, "broadcast")
        # no broadcast speakers at all: fall back to each occupied room's
        # assigned speaker, then the general speaker (v7.92.0 — replaced
        # area auto-discovery; see room_speaker()'s docstring for why)
        occupied = currently_occupied_areas(hass)
        targets = [t for a in occupied if (t := room_speaker(hass, a))]
        if not targets:
            general = general_speaker_target(hass)
            if general:
                targets = [general]
        return (targets, "broadcast") if targets else ([], "notify_only")

    # ─── MEDIUM ──────────────────────────────────────────────────────────────
    if urgency == "medium":
        if is_sleeping:
            return ([], "suppressed")
        if not home:
            return ([], "notify_only")
        occupied = currently_occupied_areas(hass)
        for area in occupied:
            assigned = room_speaker(hass, area)
            if assigned:
                return ([assigned], "local")
        if occupied:
            general = general_speaker_target(hass)
            if general:
                return ([general], "local")
        # Home but no room-level presence (could be shared state / person sensor)
        speakers = _broadcast_speakers()
        if speakers:
            return (speakers, "broadcast")
        return ([], "notify_only")

    # ─── LOW ─────────────────────────────────────────────────────────────────
    if urgency == "low":
        if is_sleeping:
            return ([], "suppressed")
        occupied = currently_occupied_areas(hass)
        if not occupied:
            return ([], "suppressed")
        for area in occupied:
            assigned = room_speaker(hass, area)
            if assigned:
                return ([assigned], "local")
        general = general_speaker_target(hass)
        if general:
            return ([general], "local")
        return ([], "suppressed")

    return ([], "suppressed")


# ─── Routing: broadcast (briefing / sentinel / doorbell) ─────────────────────

def broadcast_target(
    hass: HomeAssistant,
    *,
    broadcast_group: Optional[str] = None,
    announcement_speakers: Optional[list] = None,
) -> list[str]:
    """
    Pick speakers for explicit broadcast announcements (briefing, sentinel alert,
    doorbell, face recognition). These are always to-everyone, not room-routed.

    Priority:
      1. Configured announcement_speakers (the panel selection), validated present
      2. Configured broadcast_group entity (single entity, typically a Cast group)
      3. Empty list — SILENT.

    Silent-until-configured (v7.83.0): with neither announcement_speakers nor a
    broadcast_group set, this returns [] rather than every media player in the
    house. A fresh install must never blast all speakers (and every TV) on the
    first announcement — the user chooses their announcement speakers in
    Settings first. Broadcasting to all was the cause of the first-launch
    "it played on all 17 devices with no cancel" reports.
    """
    spk = announcement_speakers
    if isinstance(spk, str):
        try:
            import json as _json
            spk = _json.loads(spk)
        except Exception:
            spk = [s.strip() for s in spk.split(",") if s.strip()]
    if isinstance(spk, (list, tuple)):
        valid = [s for s in spk if s and hass.states.get(s) is not None]
        if valid:
            return valid

    if broadcast_group:
        state = hass.states.get(broadcast_group)
        if state is not None:
            return [broadcast_group]

    # Silent until the user configures announcement speakers / a broadcast group.
    return []


# ─── Bedroom / sleep detection helpers ───────────────────────────────────────

def is_any_bedroom_occupied(
    hass: HomeAssistant, bedroom_area_ids: Iterable[str]
) -> tuple[bool, Optional[str]]:
    """
    Return (occupied, area_id) — True + the area_id of the first bedroom
    currently reporting occupancy, or (False, None).
    """
    for area_id in bedroom_area_ids:
        if is_area_occupied(hass, area_id):
            return True, area_id
    return False, None


def get_bedroom_presence_entities(
    hass: HomeAssistant, bedroom_area_ids: Iterable[str]
) -> list[str]:
    """All occupancy sensors in any bedroom area."""
    out = []
    for area_id in bedroom_area_ids:
        out.extend(presence_entities_in_area(hass, area_id))
    return out
