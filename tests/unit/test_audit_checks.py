"""The audit's compile, import and names checks, and its report.

Each check is run against a small package written for the test; the
architecture rules are covered in test_architecture_gate.py.
"""
import builtins
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _audit():
    spec = importlib.util.spec_from_file_location("_nova_audit_checks", ROOT / "scripts" / "audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._PARSE_CACHE.clear()
    return mod


def _write(tmp_path, files: dict) -> list:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(tmp_path.rglob("*.py"))


def test_compile_check_says_where_and_why_and_writes_nothing(tmp_path):
    audit = _audit()
    files = _write(tmp_path, {"ok.py": "x = 1\n", "bad.py": "x = 1\ndef f(:\n    pass\n"})
    problems = audit.compile_gate(files)
    assert len(problems) == 1
    assert "bad.py:2: SyntaxError" in problems[0]
    assert not list(tmp_path.rglob("__pycache__"))


def test_compile_check_catches_a_late_future_import(tmp_path):
    audit = _audit()
    files = _write(tmp_path, {"late.py": "x = 1\nfrom __future__ import annotations\n"})
    assert any("late.py:2" in p for p in audit.compile_gate(files))


def test_import_check_finds_missing_modules_and_names(tmp_path):
    audit = _audit()
    files = _write(tmp_path, {
        "__init__.py": "VERSION = 1\n",
        "a.py": "x = 1\n",
        "sub/__init__.py": "",
        "sub/b.py": (
            "from ..a import x\n"            # fine
            "from ..a import y\n"            # no such name
            "from ..missing import z\n"      # no such module
            "from .. import a, VERSION\n"    # a submodule and a package name
            "from .. import nothing\n"       # neither
            "def f():\n    from .c import w\n"),   # inside a function too
    })
    problems = audit.import_gate(files)
    assert any("import y → not found in a.py" in p for p in problems)
    assert any("from ..missing → module not found" in p for p in problems)
    assert any("import nothing → unresolved" in p for p in problems)
    assert any("from .c → module not found" in p for p in problems)
    assert not any("import x" in p or "import a" in p or "VERSION" in p for p in problems)


def test_import_check_accepts_names_bound_in_blocks_and_all(tmp_path):
    audit = _audit()
    files = _write(tmp_path, {
        "__init__.py": "",
        "a.py": ("try:\n    import json as fast\nexcept ImportError:\n    fast = None\n"
                 "if True:\n    (p, [q, r]) = 1, (2, 3)\n"
                 "__all__ = ['exported']\n"),
        "b.py": "from .a import fast, p, q, r, exported\n",
    })
    assert audit.import_gate(files) == []


def test_names_check_is_reported_as_skipped_without_pyflakes(tmp_path, monkeypatch, capsys):
    audit = _audit()
    _write(tmp_path, {"__init__.py": "", "a.py": "def f():\n    return missing_name\n"})
    real_import = builtins.__import__

    def no_pyflakes(name, *args, **kwargs):
        if name.startswith("pyflakes"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyflakes)
    monkeypatch.setattr(audit.sys, "argv", ["audit.py", str(tmp_path)])
    audit.main()
    assert "NAMES    : SKIPPED (pyflakes not installed)" in capsys.readouterr().out


def test_names_check_fails_on_an_undefined_name(tmp_path, monkeypatch, capsys):
    audit = _audit()
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        return
    _write(tmp_path, {"__init__.py": "", "a.py": "def f():\n    return missing_name\n"})
    monkeypatch.setattr(audit.sys, "argv", ["audit.py", str(tmp_path)])
    assert audit.main() == 1
    out = capsys.readouterr().out
    assert "NAMES    : FAIL" in out and "missing_name" in out and "AUDIT FAILED" in out


def test_a_compatibility_module_without_a_final_newline_still_counts_its_last_line(tmp_path):
    audit = _audit()
    files = _write(tmp_path, {
        **{f"{name}.py": "" for name in audit.FACADE_LIMITS},
        "__init__.py": "",
        "pattern_analyzer.py": "\n".join(["x = 1"] * 96),
    })
    problems = audit.architecture_gate(tmp_path, files)
    assert any("pattern_analyzer.py grew to 96 lines (limit 95)" in p for p in problems)


def test_report_and_exit_codes(tmp_path, monkeypatch, capsys):
    audit = _audit()
    monkeypatch.setattr(audit.sys, "argv", ["audit.py", str(tmp_path / "nope")])
    assert audit.main() == 1
    assert "audit: component dir not found" in capsys.readouterr().out
    monkeypatch.setattr(audit.sys, "argv", ["audit.py", str(ROOT / "custom_components" / "nova")])
    assert audit.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("COMPILE  : OK (") and out[1] == "IMPORTS  : OK"
    assert out[3] == "ARCHITECTURE: OK" and out[-1] == "AUDIT CLEAN"
