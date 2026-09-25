"""Tool executors, grouped by what they do.

Each executor keeps the ``(hass, args, ...) -> str`` shape the agent has
always dispatched to; its tool definition lives in ``tool_specs`` and its
name-to-executor entry in ``registry``.
"""
