"""The source of Nova's panel websocket layer, read as one unit.

websocket.py is being split into sibling modules named ws_*.py. A test that
checks the websocket layer by reading source must therefore look at
websocket.py and every ws_*.py together, so a command, helper or constant is
found wherever it lives. Tests import from here instead of hard coding
websocket.py.

Nothing here imports Home Assistant: the files are only read and parsed.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
from typing import Iterator

COMP = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "nova"


def ws_paths() -> list[pathlib.Path]:
    """websocket.py first, then every ws_*.py in name order."""
    return [COMP / "websocket.py", *sorted(COMP.glob("ws_*.py"))]


def ws_text() -> str:
    """All websocket source joined into one string, websocket.py first."""
    return "\n".join(p.read_text(encoding="utf-8") for p in ws_paths())


def ws_top_level() -> Iterator[tuple[pathlib.Path, str, ast.stmt]]:
    """Each top level statement as (file, that file's source, node), so a
    caller can use ast.get_source_segment or compile with the right filename."""
    for path in ws_paths():
        src = path.read_text(encoding="utf-8")
        for node in ast.parse(src, filename=str(path)).body:
            yield path, src, node


def ws_tree() -> ast.Module:
    """One module whose body is the top level of every websocket file."""
    return ast.Module(body=[node for _p, _s, node in ws_top_level()], type_ignores=[])


def ws_function(name: str) -> ast.AST:
    """The top level (async) function called `name`, from whichever file has it."""
    for _path, _src, node in ws_top_level():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {[p.name for p in ws_paths()]}")


def ws_owner_name(name: str) -> str:
    """The module (websocket or a ws_* sibling) that defines the top level
    `name`, as a dotted path under custom_components.nova. A name that is only
    imported into a module does not count: this is where the code lives."""
    for path, _src, node in ws_top_level():
        found = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found = [node.name]
        elif isinstance(node, ast.Assign):
            found = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found = [node.target.id]
        if name in found:
            return f"custom_components.nova.{path.stem}"
    raise AssertionError(f"{name} is not defined in {[p.name for p in ws_paths()]}")


def ws_owner_module(name: str):
    """The imported module that owns `name`. A test that patches `name` must
    patch it here: a patch on websocket (which only re exports a moved name)
    rebinds the copy websocket holds and changes nothing for the moved code."""
    return importlib.import_module(ws_owner_name(name))
