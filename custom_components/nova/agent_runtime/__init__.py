"""The implementation behind Nova's agent (``agent.py`` is its public façade).

Import direction, one way only::

    tool_specs -> grants -> capabilities/* -> registry -> context, ha_tools
               -> dispatcher -> delegation -> loop -> agent (façade)

Capabilities never import the dispatcher or the loop. Nothing here keeps
state in ``hass.data``.
"""
