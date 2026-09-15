"""Integration-layer conftest.

The unit suite (tests/unit/) uses hand-rolled fakes for speed. This layer is for
the small number of tests that need a REAL Home Assistant instance — config-flow
validation, actual entity/service registration, the setup path — where a fake
can't credibly prove the integration loads.

It relies on `pytest-homeassistant-custom-component` (PHACC), which provides the
real `hass` fixture. PHACC is a heavy, version-pinned dependency, so it is NOT
required for the unit suite; these tests skip cleanly when it is absent.

    pip install pytest-homeassistant-custom-component

Must run in its OWN venv, never the one used for tests/unit/ — PHACC pulls in
the real `homeassistant` package, which collides with tests/unit/'s
hand-rolled fakes and sys.modules stubs (confirmed live: installing PHACC
into the shared dev venv took 1634 passing unit tests to 1465 errors).
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Skip this entire directory unless PHACC is installed.
pytest.importorskip(
    "pytest_homeassistant_custom_component",
    reason="install pytest-homeassistant-custom-component to run integration tests",
)

# PHACC requires this opt-in fixture to enable loading custom integrations.
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,  # re-exported for test modules
)


def _enable_custom_components() -> None:
    """Make `custom_components/nova` importable by `hass`.

    PHACC ships its OWN dummy `custom_components/__init__.py` package (under
    testing_config/) for its own example tests, and — confirmed live —
    something in PHACC's plugin bootstrap imports it before any conftest or
    test code runs, caching it in sys.modules. Because it's a real (non-
    namespace) package, ITS __path__ is what Home Assistant's loader scans;
    sys.path order is irrelevant once a module is already cached, so fixing
    sys.path here (tried first; kept as a no-op-if-already-there safety net)
    doesn't help on its own. __path__ is a plain mutable list even on a
    regular package, though, so appending this repo's custom_components
    directory to the ALREADY-CACHED module's __path__ makes HA's loader
    (which iterates every entry in __path__, not just the first) see
    custom_components/nova too. Without this, loader.
    async_get_custom_components(hass) returns {} and HA logs a generic
    "Integration not found" with no hint why.
    """
    if sys.path[0] != _REPO_ROOT:
        sys.path.insert(0, _REPO_ROOT)
    import custom_components
    real_dir = os.path.join(_REPO_ROOT, "custom_components")
    if real_dir not in list(custom_components.__path__):
        custom_components.__path__.append(real_dir)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """PHACC gate: makes `custom_components/nova` importable by `hass`.
    See :func:`_enable_custom_components` for why this mutation is needed."""
    _enable_custom_components()
    yield


@pytest.fixture(scope="session", autouse=True)
def _prestart_nova_log_writer():
    """Start Nova's persistent-log background thread once per session, before
    any test's PHACC thread-leak snapshot is taken.

    websocket.py's _ensure_writer() lazily starts a daemon thread ("nova-log-
    writer") the first time anything logs through nova_log() during setup.
    It's an intentional once-per-process thread with no shutdown path — it's
    meant to outlive every config entry and die only with the process, same
    as a logging handler would. PHACC's per-test cleanup fixture snapshots
    threading.enumerate() before each test and fails the test if a new thread
    is still alive after — so whichever test happened to trigger it first
    would fail teardown for a thread that was never a leak. Starting it here,
    at session scope, makes it part of every test's "before" snapshot instead.
    """
    _enable_custom_components()
    from custom_components.nova import websocket as _ws
    _ws._ensure_writer()
