#!/usr/bin/env python3
"""Nova pre-release audit.

Two checks that the old ast.parse gate missed:

  1. COMPILE  — py_compile (real bytecode compile) of every module. Unlike
     ast.parse, this enforces __future__ positioning and other compile-stage
     rules, matching how Home Assistant actually imports the integration.

  2. IMPORTS  — resolves every relative import (top-level AND lazy/nested)
     across the package and verifies each imported name actually exists in the
     target module (or its __all__). Catches wrong relative levels
     (e.g. `from .automation` inside a subpackage that needed `..automation`)
     and stale exports — failures that are invisible to per-file syntax checks.

Run from anywhere:  python3 scripts/audit.py [component_dir]
Default component dir: custom_components/nova
Exit code 0 = clean, 1 = problems found.
"""
from __future__ import annotations

import ast
import pathlib
import py_compile
import sys


def _component_dir() -> pathlib.Path:
    if len(sys.argv) > 1:
        return pathlib.Path(sys.argv[1])
    here = pathlib.Path(__file__).resolve().parent
    return here.parent / "custom_components" / "nova"


def _modules(root: pathlib.Path) -> list[pathlib.Path]:
    return [f for f in root.rglob("*.py") if "__pycache__" not in str(f)]


def compile_gate(files: list[pathlib.Path]) -> list[str]:
    problems = []
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as exc:
            problems.append(f"{f}: {str(exc).splitlines()[0]}")
    return problems


# ── relative-import resolution ────────────────────────────────────────────────
_PARSE_CACHE: dict[pathlib.Path, ast.Module] = {}


def _parse(p: pathlib.Path) -> ast.Module:
    if p not in _PARSE_CACHE:
        _PARSE_CACHE[p] = ast.parse(p.read_text())
    return _PARSE_CACHE[p]


def _toplevel_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    def add_target(tg: ast.AST) -> None:
        if isinstance(tg, ast.Name):
            names.add(tg.id)
        elif isinstance(tg, (ast.Tuple, ast.List)):
            for e in tg.elts:
                add_target(e)

    def scan(body: list[ast.stmt]) -> None:
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    add_target(t)
            elif isinstance(n, ast.AnnAssign):
                if isinstance(n.target, ast.Name):
                    names.add(n.target.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.Try):
                scan(n.body)
                for h in n.handlers:
                    scan(h.body)
                scan(n.orelse)
                scan(n.finalbody)
            elif isinstance(n, ast.If):
                scan(n.body)
                scan(n.orelse)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                scan(n.body)

    scan(tree.body)
    return names


def _dunder_all(tree: ast.Module) -> set[str] | None:
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    try:
                        return set(ast.literal_eval(n.value))
                    except Exception:
                        return None
    return None


def _resolve(curfile: pathlib.Path, level: int, module: str | None) -> pathlib.Path | None:
    base = curfile.parent
    for _ in range(level - 1):
        base = base.parent
    if module:
        parts = module.split(".")
        as_mod = base.joinpath(*parts).with_suffix(".py")
        as_pkg = base.joinpath(*parts, "__init__.py")
        if as_mod.exists():
            return as_mod
        if as_pkg.exists():
            return as_pkg
        return None
    return base / "__init__.py"


def import_gate(files: list[pathlib.Path]) -> list[str]:
    problems = []
    for f in files:
        for node in ast.walk(_parse(f)):
            if not (isinstance(node, ast.ImportFrom) and node.level and node.level >= 1):
                continue
            if node.module is None:  # from . import x, y
                base = f.parent
                for _ in range(node.level - 1):
                    base = base.parent
                for a in node.names:
                    nm = a.name
                    if (base / f"{nm}.py").exists() or (base / nm / "__init__.py").exists():
                        continue
                    ini = base / "__init__.py"
                    if ini.exists() and nm in _toplevel_names(_parse(ini)):
                        continue
                    problems.append(f"{f}: from {'.' * node.level} import {nm} → unresolved")
            else:
                target = _resolve(f, node.level, node.module)
                if target is None:
                    problems.append(f"{f}: from {'.' * node.level}{node.module} → module not found")
                    continue
                tt = _parse(target)
                allset = _dunder_all(tt)
                valid = (allset if allset is not None else set()) | _toplevel_names(tt)
                for a in node.names:
                    if a.name == "*":
                        continue
                    # `from .package import submodule` imports the submodule.
                    if target.name == "__init__.py" and (
                            (target.parent / f"{a.name}.py").exists()
                            or (target.parent / a.name / "__init__.py").exists()):
                        continue
                    if a.name not in valid:
                        problems.append(
                            f"{f}: from {'.' * node.level}{node.module} import {a.name} → not found in {target.name}"
                        )
    return problems


def undefined_names_gate(files: list[pathlib.Path]) -> list[str]:
    """Catch references to names that don't exist in scope (v6.78.1).

    py_compile happily accepts a NameError inside a nested function — it's only
    raised at runtime, and if the caller wraps it in `except Exception` it fails
    silently forever. That is exactly how a scheduled briefing shipped calling an
    undefined `groq_client`. pyflakes resolves scopes statically and catches it.

    Skipped (not failed) when pyflakes isn't installed, so the gate never blocks
    a machine that lacks it."""
    try:
        from pyflakes.api import check
        from pyflakes.reporter import Reporter
    except Exception:
        return []
    import io
    problems: list[str] = []
    for f in files:
        out, err = io.StringIO(), io.StringIO()
        try:
            check(f.read_text(), str(f), Reporter(out, err))
        except Exception:
            continue
        for line in out.getvalue().splitlines():
            # only undefined names — unused imports/vars are style, not bugs
            if "undefined name" in line:
                problems.append(line.strip())
    return problems


# ── Architecture ───────────────────────────────────────────────────────────
# Which capability package may import which. Every package may import the
# top-level core modules; leaf packages may import nothing else in Nova.
# A new edge fails the audit: add it here deliberately, in the same change,
# only when the dependency direction is intended.
ALLOWED_PACKAGE_DEPS = {
    "agent_runtime": {"automation", "diagnostics", "persistence", "providers"},
    "automation": {"cognitive", "persistence"},
    "cognitive": {"providers"},
    "diagnostics": set(),
    "intent": {"automation"},
    "providers": set(),
}
LEAF_PACKAGES = {"persistence", "audio", "vision"}

# Root compatibility modules keep public imports and patch points alive.
# They may shrink but must not grow: no new top-level definitions and no
# more lines than today. New behaviour belongs in its package.
FACADE_LIMITS = {
    "agent": (424, ("_Facade",)),
    "llm_provider": (181, ("_classify_conn_error", "_openai_style_usage",
                           "chat_with_activity", "test_connection")),
    "reasoning_loop": (248, ("_decision_from_cache", "_lm_compose", "_local_fallback",
                             "_parse_reasoning_json", "_rich_mode", "_snapshot",
                             "_structured_hazard", "_try_local_reasoning", "decide")),
    "local_mind": (370, ("_case_prior", "_compose", "_connect", "_data_days", "_ev",
                         "_is_duplicate", "_note_event", "_pick", "_security_relevant",
                         "_state_phrase", "assess", "assess_core", "compose_announcement",
                         "history_profile", "stats")),
    "automation_creator": (34, ()),
    "automation_inventory": (50, ()),
    "automation_matcher": (34, ()),
    "automation_trials": (35, ()),
    "pattern_analyzer": (95, ()),
}


def _module_name(root: pathlib.Path, f: pathlib.Path) -> str:
    parts = list(f.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_level_nodes(body: list) -> list:
    """Statements that run at import time, including inside top-level
    if/try blocks (but not function or class bodies)."""
    out = []
    for node in body:
        out.append(node)
        if isinstance(node, (ast.If, ast.Try)):
            nested = list(node.body) + list(node.orelse) + list(getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                nested += handler.body
            out += _module_level_nodes(nested)
    return out


def _import_edges(root: pathlib.Path, files: list[pathlib.Path]):
    """(import-time edges, all edges) between Nova modules. `from . import x`
    where x is a submodule points at x, not at the package."""
    mods = {_module_name(root, f): f for f in files}
    eager: dict[str, set[str]] = {m: set() for m in mods}
    every: dict[str, set[str]] = {m: set() for m in mods}
    for name, f in mods.items():
        tree = _parse(f)
        top = {id(n) for n in _module_level_nodes(tree.body)}
        base = name.split(".") if name else []
        if f.name != "__init__.py":
            base = base[:-1]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.level):
                continue
            anchor = base[:len(base) - (node.level - 1)] if node.level > 1 else base
            target = ".".join(anchor + (node.module.split(".") if node.module else []))
            for alias in node.names:
                sub = f"{target}.{alias.name}".strip(".")
                dest = sub if sub in mods else target
                if dest in mods and dest != name:
                    every[name].add(dest)
                    if id(node) in top:
                        eager[name].add(dest)
    return eager, every


def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                found.append(sorted(comp))

    for v in sorted(edges):
        if v not in index:
            visit(v)
    return found


def architecture_gate(root: pathlib.Path, files: list[pathlib.Path]) -> list[str]:
    problems = []
    eager, every = _import_edges(root, files)
    for comp in _cycles(eager):
        problems.append("import-time cycle: " + " -> ".join(m or "<package>" for m in comp))

    def package(m: str) -> str:
        head = m.split(".")[0]
        return head if "." in m or (root / head).is_dir() else ""

    for src, dests in sorted(every.items()):
        sp = package(src)
        for dest in sorted(dests):
            dp = package(dest)
            if sp == dp or not sp:
                continue
            if sp in LEAF_PACKAGES:
                problems.append(f"leaf package {sp} imports {dest or '<package>'} ({src})")
            elif not dp:
                continue
            elif dp not in ALLOWED_PACKAGE_DEPS.get(sp, set()):
                problems.append(f"{sp} may not import {dp} ({src} -> {dest})")

    for facade, (max_lines, allowed) in sorted(FACADE_LIMITS.items()):
        f = root / f"{facade}.py"
        if not f.is_file():
            problems.append(f"compatibility module {facade}.py is missing")
            continue
        lines = f.read_text(encoding="utf-8").count("\n")
        if lines > max_lines:
            problems.append(f"compatibility module {facade}.py grew to {lines} lines (limit {max_lines})")
        defs = {n.name for n in _parse(f).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        extra = sorted(defs - set(allowed))
        if extra:
            problems.append(f"compatibility module {facade}.py gained definitions: {extra}")
    return problems


def main() -> int:
    root = _component_dir()
    if not root.is_dir():
        print(f"audit: component dir not found: {root}")
        return 1
    files = _modules(root)

    compile_problems = compile_gate(files)
    import_problems = import_gate(files)
    undefined_problems = undefined_names_gate(files)
    architecture_problems = architecture_gate(root, files)

    print(f"COMPILE  : {'OK (' + str(len(files)) + ' modules)' if not compile_problems else 'FAIL'}")
    for p in compile_problems:
        print(f"  ✗ {p}")
    print(f"IMPORTS  : {'OK' if not import_problems else 'FAIL'}")
    for p in import_problems:
        print(f"  ✗ {p}")
    print(f"NAMES    : {'OK' if not undefined_problems else 'FAIL'}")
    for p in undefined_problems:
        print(f"  ✗ {p}")
    print(f"ARCHITECTURE: {'OK' if not architecture_problems else 'FAIL'}")
    for p in architecture_problems:
        print(f"  ✗ {p}")

    if compile_problems or import_problems or undefined_problems or architecture_problems:
        print("\nAUDIT FAILED")
        return 1
    print("\nAUDIT CLEAN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
