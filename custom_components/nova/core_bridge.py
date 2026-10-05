"""Call time access to cognitive core names that a core module cannot import.

The managers in core_safety.py, core_lockdown.py and the other class modules
are imported by core_state.py, and cognitive_core.py imports every core
module, so those managers cannot import the modules above them (or
cognitive_core.py) at import time. Each function here looks the real name up
in the module that owns it when it is called, like ws_bridge.py does for
websocket.py. That keeps one definition, keeps the import graph free of
cycles, and means a test that patches the name on cognitive_core (which
forwards the write to the owner) still takes effect for the caller.

Nothing runs at import time.
"""
from __future__ import annotations


def _autonomy_file(*args, **kwargs):
    from . import cognitive_core
    return cognitive_core._autonomy_file(*args, **kwargs)


def _ignore_file(*args, **kwargs):
    from . import cognitive_core
    return cognitive_core._ignore_file(*args, **kwargs)
