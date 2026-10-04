"""Call-time access to the entry and runtime helpers that live in websocket.py.

The panel commands that moved out of websocket.py still use the entry and
runtime helpers defined there (_get_entry, _executor_runtime_config, ...).
websocket.py imports the moved modules, so they cannot import it back at
import time. Each function here looks the real helper up in websocket.py when
it is called, which keeps one definition, keeps the import graph free of
cycles, and means a test that patches the helper on websocket still takes
effect for the moved commands.

Nothing runs at import time. When the entry and runtime helpers move to their
own module, these delegators go away.
"""
from __future__ import annotations


def _get_entry(hass):
    from . import websocket
    return websocket._get_entry(hass)


def _executor_runtime_config(entry) -> dict:
    from . import websocket
    return websocket._executor_runtime_config(entry)
