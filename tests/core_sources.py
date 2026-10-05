"""The source of Nova's cognitive core, read as one unit.

cognitive_core.py is being split into sibling modules named core_*.py, with
cognitive_core.py kept as the public facade. A test that checks the cognitive
core by reading source must therefore look at cognitive_core.py and every
core_*.py together, so a function, constant or service call is found wherever
it lives. Tests import from here instead of hard coding cognitive_core.py.

Nothing here imports Home Assistant: the files are only read and parsed.
"""
from __future__ import annotations

import ast
import pathlib

COMP = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "nova"


def core_paths() -> list[pathlib.Path]:
    """cognitive_core.py first, then every core_*.py in name order."""
    return [COMP / "cognitive_core.py", *sorted(COMP.glob("core_*.py"))]


def core_text() -> str:
    """All cognitive core source joined into one string, cognitive_core.py first."""
    return "\n".join(p.read_text(encoding="utf-8") for p in core_paths())


def core_tree() -> ast.Module:
    """One module whose body is the top level of every cognitive core file."""
    body: list[ast.stmt] = []
    for path in core_paths():
        body += ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
    return ast.Module(body=body, type_ignores=[])


def core_unit(rel: str) -> str:
    """A path relative to the component, with every core_*.py reported as
    cognitive_core.py: the unit a per module allow list names."""
    if "/" not in rel and rel.startswith("core_") and rel.endswith(".py"):
        return "cognitive_core.py"
    return rel
