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
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

MIN_IMAGE_SIZE: int = 2000

_FALLBACK_BASE_URL = "http://127.0.0.1:8123"


def _registry_platform(hass: Any, entity_id: str) -> Optional[str]:
    """Return the entity registry platform for ``entity_id``, or None if unknown."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.platform if entry is not None else None


class CameraBackend(abc.ABC):
    """A camera system that may own entities and fetch better snapshots."""

    name: str = "abstract"

    @abc.abstractmethod
    def handles(self, hass: Any, entity_id: str) -> bool:
        """Return True if this backend owns ``entity_id``."""

    @abc.abstractmethod
    async def fetch_best_image(
        self, hass: Any, entity_id: str, event_cache: dict
    ) -> Optional[bytes]:
        """Return a better image for ``entity_id``, or None to use the generic path."""


class _PlatformBackend(CameraBackend):
    """Backend that claims entities registered under one of ``platforms``."""

    platforms: tuple[str, ...] = ()

    def handles(self, hass: Any, entity_id: str) -> bool:
        return _registry_platform(hass, entity_id) in self.platforms


class FrigateBackend(_PlatformBackend):
    """Fetches the event snapshot via the Frigate integration's notification proxy."""

    name = "frigate"
    platforms = ("frigate",)

    async def fetch_best_image(
        self, hass: Any, entity_id: str, event_cache: dict
    ) -> Optional[bytes]:
        # Obtained up front, even when there is nothing to fetch (quirk Q2).
        session = aiohttp_client.async_get_clientsession(hass)

        try:
            base_url = network.get_url(hass, allow_internal=True, prefer_external=False)
        except Exception:  # noqa: BLE001
            base_url = _FALLBACK_BASE_URL

        cached = event_cache.get(entity_id)
        if not cached or cached.get("source") != "frigate":
            return None
        event_id = cached.get("event_id")
        if not event_id:
            return None

        url = f"{base_url}/api/frigate/notifications/{event_id}/snapshot.jpg"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                body = await resp.read()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Frigate snapshot fetch failed for %s: %s", entity_id, err)
            return None
        return body if len(body) > MIN_IMAGE_SIZE else None


class NestBackend(_PlatformBackend):
    """Fetches event media from the Nest integration's subscriber media stores."""

    name = "nest"
    platforms = ("nest",)

    async def fetch_best_image(
        self, hass: Any, entity_id: str, event_cache: dict
    ) -> Optional[bytes]:
        cached = event_cache.get(entity_id)
        if not cached or cached.get("source") != "nest":
            return None
        device_id = cached.get("device_id")
        event_id = cached.get("event_id")
        if not device_id or not event_id:
            return None

        try:
            nest_data = hass.data.get("nest")
            if not nest_data:
                return None
            # Current HA stores a dict here, which has no `subscribers` (quirk Q1).
            for subscriber in getattr(nest_data, "subscribers", []):
                store = getattr(subscriber, "media_store", None)
                if not store:
                    continue
                try:
                    media = await store.async_get_media(device_id, event_id)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Nest media fetch failed for %s: %s", entity_id, err)
                    continue
                contents = getattr(media, "contents", None)
                if contents and len(contents) > MIN_IMAGE_SIZE:
                    return contents
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Nest backend error for %s: %s", entity_id, err)
        return None


class UniFiProtectBackend(_PlatformBackend):
    """Placeholder: claims UniFi Protect cameras but uses the generic snapshot."""

    name = "unifiprotect"
    platforms = ("unifiprotect", "unifi_protect")

    async def fetch_best_image(
        self, hass: Any, entity_id: str, event_cache: dict
    ) -> Optional[bytes]:
        return None


class ReolinkBackend(_PlatformBackend):
    """Placeholder: claims Reolink cameras but uses the generic snapshot."""

    name = "reolink"
    platforms = ("reolink",)

    async def fetch_best_image(
        self, hass: Any, entity_id: str, event_cache: dict
    ) -> Optional[bytes]:
        return None


BACKENDS: list[CameraBackend] = [
    FrigateBackend(), NestBackend(), UniFiProtectBackend(), ReolinkBackend(),
]


def find_backend(hass: Any, entity_id: str) -> Optional[CameraBackend]:
    """Return the first backend in ``BACKENDS`` that owns ``entity_id``, or None."""
    for backend in BACKENDS:
        try:
            if backend.handles(hass, entity_id):
                return backend
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Backend %s ownership check failed for %s: %s",
                          backend.name, entity_id, err)
    return None
