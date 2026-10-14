"""
Nova — choosing which speaker to talk through.

Home Assistant's registries say which area an entity sits in. Which speaker
plays in that area is never guessed: each room has at most one speaker the
user assigned to it (room_speakers), and one general speaker
(general_speaker) covers rooms with none. Satellites are listening devices
and only speak as a last resort for a reply. Occupancy, motion and presence
binary sensors tell Nova which rooms have someone in them.

Settings read here:
  room_speakers         {area_id: media_player}
  general_speaker       media_player used when a room has none
  announcement_speakers / broadcast_group
                        the whole house speakers; nothing is spoken there
                        until one of them is set
  announce_notify_only  send proactive announcements to phones instead

Replies go to the panel pairing for the satellite, else the room speaker,
else the general speaker, else the satellite itself.

Observer announcements by urgency:
  critical  whole house speakers, even while asleep; phones when none are set
  high      whole house when awake and someone is home, else phones; with no
            whole house speakers, the occupied rooms' speakers, then the
            general speaker
  medium    asleep: held back. Away: phones. Otherwise the occupied room's
            speaker, then the general speaker if a room is occupied, then
            the whole house speakers, then phones
  low       asleep or no occupied room: held back. Otherwise the occupied
            room's speaker, then the general speaker

Every spoken path also passes its targets through drop_display_targets so a
television is never spoken to.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .entity_filter import is_object_sensor

_LOGGER = logging.getLogger(__name__)

_PRESENCE_CLASSES = ("occupancy", "motion", "presence")
_OCCUPIED_WORDS = frozenset({"on", "home", "detected", "true", "occupied"})


# ─── Lookups ────────────────────────────────────────────────────────────────

def entity_area(hass: HomeAssistant, entity_id: str) -> Optional[str]:
    """The entity's own area, else its device's area, else None."""
    try:
        entry = er.async_get(hass).async_get(entity_id)
        if entry is None:
            return None
        if entry.area_id:
            return entry.area_id
        if not entry.device_id:
            return None
        device = dr.async_get(hass).async_get(entry.device_id)
        return (device.area_id or None) if device else None
    except Exception as exc:
        _LOGGER.debug("area lookup failed for %s: %s", entity_id, exc)
        return None


def _entities_by_domain(hass: HomeAssistant, domain: str) -> list[str]:
    return [state.entity_id for state in hass.states.async_all(domain)]


def _setting(key: str, default):
    try:
        from . import nova_config
        return nova_config.get(key, default)
    except Exception:
        return default


def _present(hass: HomeAssistant, entity_id) -> Optional[str]:
    """The entity id when it is set and Home Assistant knows it."""
    if entity_id and hass.states.get(entity_id) is not None:
        return entity_id
    return None


def satellites_in_area(hass: HomeAssistant, area_id: str) -> list[str]:
    return [eid for eid in _entities_by_domain(hass, "assist_satellite")
            if entity_area(hass, eid) == area_id]


def room_speaker(hass: HomeAssistant, area_id: str) -> Optional[str]:
    """The speaker the user assigned to this area, while it exists.

    Nothing is discovered from the area itself: integrations can register
    several media players for one television, and one of them could slip
    past any rule that tries to filter them out."""
    assigned = (_setting("room_speakers", {}) or {}).get(area_id)
    return _present(hass, assigned)


def general_speaker_target(hass: HomeAssistant) -> Optional[str]:
    """The speaker for rooms with none assigned, while it exists. Whole
    house broadcasts use their own settings and ignore it."""
    return _present(hass, _setting("general_speaker", "") or "")


# ─── Television guard ───────────────────────────────────────────────────────

def _is_display_target(hass: HomeAssistant, entity_id: str, movie: str) -> bool:
    """A television or the chosen movie player: never a speech target."""
    if not entity_id:
        return False
    if movie and entity_id == movie:
        return True
    state = hass.states.get(entity_id)
    return state is not None and state.attributes.get("device_class") == "tv"


def drop_display_targets(hass: HomeAssistant, targets, context: str = "") -> list[str]:
    """Remove televisions and the movie player from a list of speech targets,
    however the list was built. Anything removed is logged as a warning
    naming the caller, so the route that picked a television can be found.

    The movie player comes from the live runtime config rather than
    nova_config, so reading it never triggers a config file load."""
    from .runtime import domain_runtime_config
    movie = domain_runtime_config(hass).get("movie_media_player") or ""
    kept: list[str] = []
    dropped: list[str] = []
    for target in targets or ():
        (dropped if _is_display_target(hass, target, movie) else kept).append(target)
    if dropped:
        _LOGGER.warning(
            "Nova: refused to send TTS to display target(s) %s (context=%s) — "
            "a routing path tried to speak through a TV; speaking to %s instead",
            dropped, context or "?", kept or "nothing")
    return kept


# ─── Presence ───────────────────────────────────────────────────────────────

def _exclusion_check():
    try:
        from .entity_filter import is_excluded
        return is_excluded
    except Exception:
        return lambda _hass, _eid: False


def _counts_as_presence(state) -> bool:
    """A person detector: right device class, and not a car, pet or parcel
    sensor."""
    return (state.attributes.get("device_class") in _PRESENCE_CLASSES
            and not is_object_sensor(state.entity_id,
                                     state.attributes.get("friendly_name")))


def _is_on(state) -> bool:
    return str(state.state).lower() in _OCCUPIED_WORDS


def presence_entities_in_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """Presence sensors in this area that the user has not excluded."""
    skip = _exclusion_check()
    found = []
    for eid in _entities_by_domain(hass, "binary_sensor"):
        if skip(hass, eid):
            continue
        state = hass.states.get(eid)
        if state is None or not _counts_as_presence(state):
            continue
        if entity_area(hass, eid) == area_id:
            found.append(eid)
    return found


def all_areas_with_satellite(hass: HomeAssistant) -> set[str]:
    """Every area Nova can hear from."""
    areas = (entity_area(hass, eid) for eid in _entities_by_domain(hass, "assist_satellite"))
    return {area for area in areas if area}


def all_areas_with_presence(hass: HomeAssistant) -> set[str]:
    """Every area with a presence sensor the user has not excluded."""
    skip = _exclusion_check()
    found: set[str] = set()
    for state in hass.states.async_all("binary_sensor"):
        if skip(hass, state.entity_id) or not _counts_as_presence(state):
            continue
        area = entity_area(hass, state.entity_id)
        if area:
            found.add(area)
    return found


def is_area_occupied(hass: HomeAssistant, area_id: str) -> bool:
    for eid in presence_entities_in_area(hass, area_id):
        state = hass.states.get(eid)
        if state is not None and _is_on(state):
            return True
    return False


def currently_occupied_areas(hass: HomeAssistant) -> list[str]:
    return [area for area in all_areas_with_presence(hass)
            if is_area_occupied(hass, area)]


def anyone_home(hass: HomeAssistant) -> bool:
    """Whether anyone seems to be home: a presence sensor the user has not
    excluded that is on anywhere (with or without an area), or a person or
    device tracker at home."""
    skip = _exclusion_check()
    for state in hass.states.async_all("binary_sensor"):
        if (_counts_as_presence(state) and _is_on(state)
                and not skip(hass, state.entity_id)):
            return True
    return any(str(state.state).lower() == "home"
               for domain in ("person", "device_tracker")
               for state in hass.states.async_all(domain))


def is_any_bedroom_occupied(
    hass: HomeAssistant, bedroom_area_ids: Iterable[str],
) -> tuple[bool, Optional[str]]:
    """(True, area) for the first occupied bedroom, else (False, None)."""
    for area in bedroom_area_ids:
        if is_area_occupied(hass, area):
            return True, area
    return False, None


# ─── Replies ────────────────────────────────────────────────────────────────

def _device_satellite(hass: HomeAssistant, device_id: str):
    """(area, satellite entity) for a device id; either may be None."""
    area = satellite = None
    try:
        device = dr.async_get(hass).async_get(device_id)
        if device:
            area = device.area_id
        for entry in er.async_get(hass).entities.values():
            if entry.device_id == device_id and entry.domain == "assist_satellite":
                satellite = entry.entity_id
                break
    except Exception as exc:
        _LOGGER.debug("device_id->area lookup failed: %s", exc)
    return area, satellite


def reply_target(
    hass: HomeAssistant, *,
    satellite_entity_id: Optional[str] = None, device_id: Optional[str] = None,
    satellite_pairings: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """The one speaker for a reply to someone who spoke to a satellite.

    In order: the speaker paired with the satellite in the panel (while it
    exists), the room speaker for the satellite's area, the general
    speaker, then the satellite itself. A satellite whose area is unknown
    replies through itself. Pass the satellite entity, or a device id to
    look it up from."""
    satellite, area = satellite_entity_id, None
    if satellite_entity_id:
        area = entity_area(hass, satellite_entity_id)
    elif device_id:
        area, found = _device_satellite(hass, device_id)
        satellite = found or satellite

    _LOGGER.debug("reply_target: satellite=%s device=%s area=%s pairings=%s",
                  satellite, device_id, area,
                  list(satellite_pairings) if satellite_pairings else None)
    if satellite_pairings and satellite and satellite in satellite_pairings:
        paired = satellite_pairings[satellite]
        if paired and hass.states.get(paired):
            _LOGGER.debug("reply_target: paired speaker %s", paired)
            return paired
        _LOGGER.debug("reply_target: paired speaker %s is unavailable", paired)

    if not area:
        _LOGGER.debug("reply_target: satellite area unknown")
        return satellite
    choice = room_speaker(hass, area) or general_speaker_target(hass)
    _LOGGER.debug("reply_target: area '%s' resolves to %s", area,
                  choice or "the satellite")
    return choice or satellite


def reply_targets(
    hass: HomeAssistant, *,
    device_id: Optional[str] = None,
    voice_satellites: Optional[list] = None, reply_speakers: Optional[list] = None,
    broadcast_speakers: Optional[list] = None, legacy_cast_speakers: Optional[list] = None,
    room_routing: bool = True,
    satellite_pairings: Optional[dict[str, str]] = None,
) -> list[str]:
    """Older list form of reply_target. The speaker list arguments and
    room_routing are accepted and ignored. Without a device id, or when the
    device resolves to nothing but its own satellite, the broadcast
    speakers are returned."""
    found = None
    if device_id:
        found = reply_target(hass, device_id=device_id,
                             satellite_pairings=satellite_pairings)
    if found and not found.startswith("assist_satellite."):
        return [found]
    return broadcast_target(hass)


# ─── Observer announcements ─────────────────────────────────────────────────

def notify_only_enabled(hass: HomeAssistant) -> bool:
    """The announce_notify_only setting; unreadable counts as off."""
    try:
        from . import nova_config
        return bool(nova_config.announce_notify_only(hass))
    except Exception:
        return False


def observer_speak_target(
    hass: HomeAssistant, *, urgency: str,
    broadcast_group: Optional[str] = None,
    announcement_speakers: Optional[list[str]] = None,
    is_sleeping: bool = False,
    authoritative_anyone_home: Optional[bool] = None,
) -> tuple[list[str], str]:
    """(targets, mode) for an observer announcement; see _observer_route.
    With announce_notify_only on, a spoken route ("local" or "broadcast")
    below critical becomes ([], "notify_only")."""
    targets, mode = _observer_route(
        hass, urgency=urgency, broadcast_group=broadcast_group,
        announcement_speakers=announcement_speakers, is_sleeping=is_sleeping,
        authoritative_anyone_home=authoritative_anyone_home)
    spoken = mode in ("local", "broadcast")
    if spoken and urgency != "critical" and notify_only_enabled(hass):
        return ([], "notify_only")
    return (targets, mode)


def _first_room_speaker(hass: HomeAssistant, areas: list[str]) -> Optional[str]:
    for area in areas:
        assigned = room_speaker(hass, area)
        if assigned:
            return assigned
    return None


def _observer_route(
    hass: HomeAssistant, *, urgency: str,
    broadcast_group: Optional[str] = None,
    announcement_speakers: Optional[list[str]] = None,
    is_sleeping: bool = False,
    authoritative_anyone_home: Optional[bool] = None,
) -> tuple[list[str], str]:
    """Where an observer announcement goes, as (targets, mode). The mode is
    "local", "broadcast", "notify_only" (phones only) or "suppressed".

    Whole house speakers are the existing announcement_speakers, else an
    existing broadcast_group, else none at all. The per urgency rules are in
    the module docstring.

    authoritative_anyone_home, when given, is the caller's own answer to
    "is anyone home" and is used instead of anyone_home(). Motion sensors
    can stay on for a while after the last person's tracker leaves, which
    once sent a departure notice to a speaker in an empty house. Room
    occupancy still comes from the live sensors either way."""

    def whole_house() -> list[str]:
        if announcement_speakers:
            live = [s for s in announcement_speakers if hass.states.get(s)]
            if live:
                return live
        if broadcast_group and hass.states.get(broadcast_group):
            return [broadcast_group]
        return []

    if urgency == "critical":
        speakers = whole_house()
        return (speakers, "broadcast") if speakers else ([], "notify_only")

    home = (anyone_home(hass) if authoritative_anyone_home is None
            else authoritative_anyone_home)

    if urgency == "low":
        rooms = [] if is_sleeping else currently_occupied_areas(hass)
        if not rooms:
            return ([], "suppressed")
        local = _first_room_speaker(hass, rooms) or general_speaker_target(hass)
        return ([local], "local") if local else ([], "suppressed")

    if urgency not in ("high", "medium"):
        return ([], "suppressed")

    if urgency == "high":
        if is_sleeping or not home:
            return ([], "notify_only")
        speakers = whole_house()
        if not speakers:
            rooms = currently_occupied_areas(hass)
            speakers = [s for s in (room_speaker(hass, a) for a in rooms) if s]
            if not speakers:
                general = general_speaker_target(hass)
                speakers = [general] if general else []
        return (speakers, "broadcast") if speakers else ([], "notify_only")

    # medium
    if is_sleeping:
        return ([], "suppressed")
    if not home:
        return ([], "notify_only")
    rooms = currently_occupied_areas(hass)
    local = _first_room_speaker(hass, rooms)
    if local is None and rooms:
        local = general_speaker_target(hass)
    if local:
        return ([local], "local")
    speakers = whole_house()
    return (speakers, "broadcast") if speakers else ([], "notify_only")


# ─── Broadcasts ─────────────────────────────────────────────────────────────

def _speaker_list(value):
    """announcement_speakers as stored: a list, a JSON list, or comma text."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return [part.strip() for part in value.split(",") if part.strip()]


def broadcast_target(
    hass: HomeAssistant, *,
    broadcast_group: Optional[str] = None,
    announcement_speakers: Optional[list] = None,
) -> list[str]:
    """Speakers for a whole house announcement (briefing, sentinel,
    doorbell, recognition): the announcement speakers that exist, else the
    broadcast group if it exists, else nothing. Nothing is spoken until the
    user picks speakers, so a new install never plays on every device."""
    speakers = _speaker_list(announcement_speakers)
    if isinstance(speakers, (list, tuple)):
        live = [s for s in speakers if _present(hass, s)]
        if live:
            return live
    group = _present(hass, broadcast_group)
    return [group] if group else []
