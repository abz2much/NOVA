"""Camera-system backends that can supply a better snapshot than HA's generic one.

Each backend decides whether it owns a camera entity (by the entity registry
platform) and may fetch a richer, event-specific image. ``camera.py`` asks
:func:`find_backend` for the owner and falls back to the generic snapshot
whenever the backend returns ``None``.
"""
from __future__ import annotations

import abc
import logging
from typing import Any, Optional

import aiohttp
from homeassistant.helpers import aiohttp_client, network
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

MIN_IMAGE_SIZE: int = 2000

_FALLBACK_BASE_URL = "http://127.0.0.1:8123"
_FRIGATE_TIMEOUT_S = 10


def _registry_platform(hass: Any, entity_id: str) -> Optional[str]:
    """Return the entity registry platform for ``entity_id``, or None if unknown."""
    entry = er.async_get(hass).async_get(entity_id)
    return None if entry is None else entry.platform


def _big_enough(contents: Any) -> Optional[bytes]:
    """``contents`` when larger than a placeholder image would be, else None."""
    return contents if contents and len(contents) > MIN_IMAGE_SIZE else None


def _cached_event(event_cache: dict, entity_id: str, source: str) -> dict:
    """The cached event for ``entity_id`` when ``source`` reported it, else {}."""
    event = event_cache.get(entity_id)
    return event if event and event.get("source") == source else {}


class CameraBackend(abc.ABC):
    """A camera system that may own entities and fetch better snapshots."""

    name: str = "abstract"

    @abc.abstractmethod
    def handles(self, hass: Any, entity_id: str) -> bool:
        """Return True if this backend owns ``entity_id``."""

    @abc.abstractmethod
    async def fetch_best_image(self, hass: Any, entity_id: str,
                               event_cache: dict) -> Optional[bytes]:
        """Return a better image for ``entity_id``, or None to use the generic path."""


class _PlatformBackend(CameraBackend):
    """Owns the entities registered under one of ``platforms``."""

    platforms: tuple[str, ...] = ()

    def handles(self, hass: Any, entity_id: str) -> bool:
        return _registry_platform(hass, entity_id) in self.platforms


class _GenericSnapshotBackend(_PlatformBackend):
    """Claims its cameras but always leaves the image to the generic snapshot."""

    async def fetch_best_image(self, hass: Any, entity_id: str,
                               event_cache: dict) -> Optional[bytes]:
        return None


def _frigate_base_url(hass: Any) -> str:
    try:
        return network.get_url(hass, allow_internal=True, prefer_external=False)
    except Exception:  # noqa: BLE001 - no usable URL configured
        return _FALLBACK_BASE_URL


class FrigateBackend(_PlatformBackend):
    """Fetches the event snapshot via the Frigate integration's notification proxy."""

    name = "frigate"
    platforms = ("frigate",)

    async def fetch_best_image(self, hass: Any, entity_id: str,
                               event_cache: dict) -> Optional[bytes]:
        # The session and base URL come first, even when the cache holds
        # nothing to fetch (quirk Q2, kept as is).
        session = aiohttp_client.async_get_clientsession(hass)
        base_url = _frigate_base_url(hass)
        event_id = _cached_event(event_cache, entity_id, "frigate").get("event_id")
        if not event_id:
            return None

        snapshot_url = f"{base_url}/api/frigate/notifications/{event_id}/snapshot.jpg"
        limit = aiohttp.ClientTimeout(total=_FRIGATE_TIMEOUT_S)
        try:
            async with session.get(snapshot_url, timeout=limit) as resp:
                body = await resp.read() if resp.status == 200 else b""
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Frigate snapshot fetch failed for %s: %s", entity_id, err)
            return None
        return _big_enough(body)


def _nest_device_name(hass: Any, device_id: str) -> Optional[str]:
    """The Nest API name of the HA device, from its ("nest", name) identifier."""
    entry = dr.async_get(hass).async_get(device_id)
    names = (str(n) for d, n in getattr(entry, "identifiers", None) or () if d == "nest")
    return next(names, None)


def _nest_sdm_device(hass: Any, nest_id: str) -> Any:
    """The device a loaded Nest entry's device manager holds under ``nest_id``."""
    for entry in hass.config_entries.async_entries("nest"):
        manager = getattr(getattr(entry, "runtime_data", None), "device_manager", None)
        device = None if manager is None else manager.devices.get(nest_id)
        if device is not None:
            return device
    return None


async def _media_from_store(store: Any, device_id: str, event_id: str,
                            entity_id: str) -> Optional[bytes]:
    try:
        media = await store.async_get_media(device_id, event_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Nest media fetch failed for %s: %s", entity_id, err)
        return None
    return _big_enough(getattr(media, "contents", None))


class NestBackend(_PlatformBackend):
    """Fetches the Nest event's own image from the Nest integration."""

    name = "nest"
    platforms = ("nest",)

    async def fetch_best_image(self, hass: Any, entity_id: str,
                               event_cache: dict) -> Optional[bytes]:
        event = _cached_event(event_cache, entity_id, "nest")
        device_id, event_id = event.get("device_id"), event.get("event_id")
        if not (device_id and event_id):
            return None

        try:
            # Current HA: the Nest device in a loaded entry's device manager
            # serves the event's media from its token. Once that device is
            # found, its answer is final.
            nest_id = _nest_device_name(hass, device_id)
            device = _nest_sdm_device(hass, nest_id) if nest_id else None
            if device is not None:
                media = await device.event_media_manager.get_media_from_token(event_id)
                return _big_enough(getattr(media, "contents", None))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Nest event media unavailable for %s: %s", entity_id, err)

        try:
            # Older HA kept subscribers with media stores in hass.data.
            nest_data = hass.data.get("nest")
            subscribers = getattr(nest_data, "subscribers", []) if nest_data else ()
            for store in (getattr(sub, "media_store", None) for sub in subscribers):
                image = await _media_from_store(store, device_id, event_id,
                                                entity_id) if store else None
                if image:
                    return image
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Nest backend error for %s: %s", entity_id, err)
        return None


class UniFiProtectBackend(_GenericSnapshotBackend):
    """Placeholder: claims UniFi Protect cameras but uses the generic snapshot."""

    name = "unifiprotect"
    platforms = ("unifiprotect", "unifi_protect")


class ReolinkBackend(_GenericSnapshotBackend):
    """Placeholder: claims Reolink cameras but uses the generic snapshot."""

    name = "reolink"
    platforms = ("reolink",)


BACKENDS: list[CameraBackend] = [
    FrigateBackend(), NestBackend(), UniFiProtectBackend(), ReolinkBackend(),
]


def _owns(backend: CameraBackend, hass: Any, entity_id: str) -> bool:
    try:
        return bool(backend.handles(hass, entity_id))
    except Exception as err:  # noqa: BLE001 - one broken backend must not stop the rest
        _LOGGER.debug("Backend %s ownership check failed for %s: %s",
                      backend.name, entity_id, err)
        return False


def find_backend(hass: Any, entity_id: str) -> Optional[CameraBackend]:
    """Return the first backend in ``BACKENDS`` that owns ``entity_id``, or None."""
    return next((b for b in BACKENDS if _owns(b, hass, entity_id)), None)
