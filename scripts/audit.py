#!/usr/bin/env python3
"""Static checks run before every release and in CI.

    python3 scripts/audit.py [component_dir]

The component directory defaults to custom_components/nova. Four checks run
over every module in it:

  COMPILE       Every module compiles to bytecode, as it must for Home
                Assistant to import it. Stricter than a plain parse: a
                misplaced __future__ import fails here. Nothing is written.
  IMPORTS       Every relative import, at the top of a module or inside a
                function, points at a module that exists and names something
                that module defines. A wrong number of dots or a stale name
                otherwise only shows when that line runs.
  NAMES         pyflakes finds names used but never defined, which compile
                accepts and which only fail when the code runs. Skipped, and
                reported as skipped, when pyflakes is not installed.
  ARCHITECTURE  No import cycles at import time, packages import only in
                the allowed direction, and the compatibility modules do not
                grow.

Prints one line per check and each problem under it, then AUDIT CLEAN (exit
0) or AUDIT FAILED (exit 1).
"""
from __future__ import annotations

import ast
import io
import pathlib
import sys

_PARSE_CACHE: dict[pathlib.Path, ast.Module] = {}


def _component_dir() -> pathlib.Path:
    if len(sys.argv) > 1:
        return pathlib.Path(sys.argv[1])
    return pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "nova"


def _modules(root: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _parse(path: pathlib.Path) -> ast.Module:
    tree = _PARSE_CACHE.get(path)
    if tree is None:
        tree = _PARSE_CACHE[path] = ast.parse(path.read_text(encoding="utf-8"))
    return tree


# ── COMPILE ──────────────────────────────────────────────────────────────────

def compile_gate(files: list[pathlib.Path]) -> list[str]:
    """Compiles each module in memory, so no .pyc lands in the source tree,
    and reports where it fails and why."""
    problems = []
    for path in files:
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except SyntaxError as err:
            problems.append(f"{path}:{err.lineno}: {type(err).__name__}: {err.msg}")
        except Exception as err:   # null bytes, a bad source encoding
            problems.append(f"{path}: {type(err).__name__}: {err}")
    return problems


# ── IMPORTS ──────────────────────────────────────────────────────────────────

def _bound_names(stmts: list[ast.stmt], into: set[str]) -> set[str]:
    """Names a module binds when it is imported: definitions, assignments and
    imports, including those inside top-level if, try and with blocks."""
    def targets(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            into.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for part in node.elts:
                targets(part)

    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            into.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                targets(target)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            into.add(stmt.target.id)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            into.update(alias.asname or alias.name.split(".")[0] for alias in stmt.names)
        elif isinstance(stmt, ast.If):
            _bound_names(stmt.body + stmt.orelse, into)
        elif isinstance(stmt, ast.Try):
            _bound_names(stmt.body + stmt.orelse + stmt.finalbody, into)
            for handler in stmt.handlers:
                _bound_names(handler.body, into)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _bound_names(stmt.body, into)
    return into


def _toplevel_names(tree: ast.Module) -> set[str]:
    return _bound_names(tree.body, set())


def _dunder_all(tree: ast.Module) -> set[str] | None:
    """The first top-level ``__all__ = [...]``, or None when absent or not a literal."""
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
            try:
                return set(ast.literal_eval(stmt.value))
            except Exception:
                return None
    return None


def _package_dir(path: pathlib.Path, level: int) -> pathlib.Path:
    """The directory a relative import with this many dots starts from."""
    return path.parents[level - 1]


def _is_submodule(directory: pathlib.Path, name: str) -> bool:
    return (directory / f"{name}.py").exists() or (directory / name / "__init__.py").exists()


def _resolve(path: pathlib.Path, level: int, module: str | None) -> pathlib.Path | None:
    start = _package_dir(path, level)
    if not module:
        return start / "__init__.py"
    parts = module.split(".")
    for candidate in (start.joinpath(*parts).with_suffix(".py"), start.joinpath(*parts, "__init__.py")):
        if candidate.exists():
            return candidate
    return None


def import_gate(files: list[pathlib.Path]) -> list[str]:
    problems = []
    for path in files:
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            dots = "." * node.level
            if node.module is None:
                # from . import x: a sibling module, or a name the package defines.
                start = _package_dir(path, node.level)
                init = start / "__init__.py"
                for alias in node.names:
                    if _is_submodule(start, alias.name):
                        continue
                    if init.exists() and alias.name in _toplevel_names(_parse(init)):
                        continue
                    problems.append(f"{path}: from {dots} import {alias.name} → unresolved")
                continue
            target = _resolve(path, node.level, node.module)
            if target is None:
                problems.append(f"{path}: from {dots}{node.module} → module not found")
                continue
            tree = _parse(target)
            known = (_dunder_all(tree) or set()) | _toplevel_names(tree)
            for alias in node.names:
                if alias.name == "*" or alias.name in known:
                    continue
                # from .package import submodule
                if target.name == "__init__.py" and _is_submodule(target.parent, alias.name):
                    continue
                problems.append(f"{path}: from {dots}{node.module} import {alias.name} → not found in {target.name}")
    return problems


# ── NAMES ────────────────────────────────────────────────────────────────────

def undefined_names_gate(files: list[pathlib.Path]) -> list[str] | None:
    """pyflakes' undefined-name reports, or None when pyflakes is missing.

    A NameError inside a function compiles fine and, behind an
    ``except Exception``, can fail quietly for ever: a scheduled briefing once
    shipped calling a client that did not exist. Unused imports and the like
    are style, so only undefined names count."""
    try:
        from pyflakes.api import check
        from pyflakes.reporter import Reporter
    except Exception:
        return None
    problems = []
    for path in files:
        found, errors = io.StringIO(), io.StringIO()
        try:
            check(path.read_text(encoding="utf-8"), str(path), Reporter(found, errors))
        except Exception:
            continue
        problems += [line.strip() for line in found.getvalue().splitlines() if "undefined name" in line]
    return problems


# ── ARCHITECTURE ─────────────────────────────────────────────────────────────
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
    # 8.7.17: split into core_*.py. The persisted path helpers stay here as
    # the storage contract (tests/fixtures/contracts/storage.json).
    "cognitive_core": (341, ("_Facade", "_autonomy_file", "_ignore_file",
                             "_lockdown_state_path", "_patterns_db")),
    "llm_provider": (181, ("_classify_conn_error", "_openai_style_usage",
                           "chat_with_activity", "test_connection")),
    "reasoning_loop": (248, ("_decision_from_cache", "_lm_compose", "_local_fallback",
                             "_parse_reasoning_json", "_rich_mode", "_snapshot",
                             "_structured_hazard", "_try_local_reasoning", "decide")),
    # 371: +1 for `from . import paths` (v8.5.0, config dir no longer hard coded).
    "local_mind": (371, ("_case_prior", "_compose", "_connect", "_data_days", "_ev",
                         "_is_duplicate", "_note_event", "_pick", "_security_relevant",
                         "_state_phrase", "assess", "assess_core", "compose_announcement",
                         "history_profile", "stats")),
    "automation_creator": (34, ()),
    "automation_inventory": (50, ()),
    "automation_matcher": (34, ()),
    "automation_trials": (35, ()),
    "pattern_analyzer": (95, ()),
}


def _module_name(root: pathlib.Path, path: pathlib.Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_time_nodes(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """Statements run when the module is imported, including those inside
    top-level if and try blocks, but not function or class bodies."""
    out = []
    for stmt in stmts:
        out.append(stmt)
        if isinstance(stmt, ast.If):
            out += _import_time_nodes(stmt.body + stmt.orelse)
        elif isinstance(stmt, ast.Try):
            out += _import_time_nodes(stmt.body + stmt.orelse + stmt.finalbody
                                      + [s for h in stmt.handlers for s in h.body])
    return out


def _import_edges(root: pathlib.Path, files: list[pathlib.Path]):
    """Edges between Nova modules: (import time only, all). ``from . import x``
    where x is a submodule points at x, not at the package."""
    modules = {_module_name(root, p): p for p in files}
    eager: dict[str, set[str]] = {m: set() for m in modules}
    every: dict[str, set[str]] = {m: set() for m in modules}
    for name, path in modules.items():
        tree = _parse(path)
        at_import = {id(n) for n in _import_time_nodes(tree.body)}
        package = name.split(".") if name else []
        if path.name != "__init__.py":
            package = package[:-1]
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            anchor = package[:len(package) - (node.level - 1)] if node.level > 1 else package
            target = ".".join(anchor + (node.module.split(".") if node.module else []))
            for alias in node.names:
                sub = f"{target}.{alias.name}".strip(".")
                dest = sub if sub in modules else target
                if dest in modules and dest != name:
                    every[name].add(dest)
                    if id(node) in at_import:
                        eager[name].add(dest)
    return eager, every


def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected groups of more than one module (Tarjan)."""
    order: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    groups: list[list[str]] = []

    def visit(v: str) -> None:
        order[v] = low[v] = len(order)
        stack.append(v)
        for w in edges[v]:
            if w not in order:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in stack:
                low[v] = min(low[v], order[w])
        if low[v] == order[v]:
            group = []
            while True:
                w = stack.pop()
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for v in sorted(edges):
        if v not in order:
            visit(v)
    return groups


def architecture_gate(root: pathlib.Path, files: list[pathlib.Path]) -> list[str]:
    eager, every = _import_edges(root, files)
    problems = ["import-time cycle: " + " -> ".join(m or "<package>" for m in group) for group in _cycles(eager)]

    def package_of(module: str) -> str:
        head = module.split(".")[0]
        return head if "." in module or (root / head).is_dir() else ""

    for src, dests in sorted(every.items()):
        src_pkg = package_of(src)
        if not src_pkg:
            continue
        for dest in sorted(dests):
            dest_pkg = package_of(dest)
            if dest_pkg == src_pkg:
                continue
            if src_pkg in LEAF_PACKAGES:
                problems.append(f"leaf package {src_pkg} imports {dest or '<package>'} ({src})")
            elif dest_pkg and dest_pkg not in ALLOWED_PACKAGE_DEPS.get(src_pkg, set()):
                problems.append(f"{src_pkg} may not import {dest_pkg} ({src} -> {dest})")

    for facade, (max_lines, allowed) in sorted(FACADE_LIMITS.items()):
        path = root / f"{facade}.py"
        if not path.is_file():
            problems.append(f"compatibility module {facade}.py is missing")
            continue
        # Every line counts, the last one too when it has no newline.
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > max_lines:
            problems.append(f"compatibility module {facade}.py grew to {lines} lines (limit {max_lines})")
        defined = {n.name for n in _parse(path).body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        extra = sorted(defined - set(allowed))
        if extra:
            problems.append(f"compatibility module {facade}.py gained definitions: {extra}")
    return problems


# ── report ───────────────────────────────────────────────────────────────────

def main() -> int:
    component = _component_dir()
    if not component.is_dir():
        print(f"audit: component dir not found: {component}")
        return 1
    files = _modules(component)
    names = undefined_names_gate(files)
    results = [
        ("COMPILE  ", compile_gate(files), f"OK ({len(files)} modules)"),
        ("IMPORTS  ", import_gate(files), "OK"),
        ("NAMES    ", names or [], "OK" if names is not None else "SKIPPED (pyflakes not installed)"),
        ("ARCHITECTURE", architecture_gate(component, files), "OK"),
    ]
    for label, problems, passed in results:
        print(f"{label}: {'FAIL' if problems else passed}")
        for problem in problems:
            print(f"  ✗ {problem}")
    failed = any(problems for _, problems, _ in results)
    print("\nAUDIT FAILED" if failed else "\nAUDIT CLEAN")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
