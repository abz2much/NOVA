"""Phase 11: the audit's architecture gate.

Each rule is proven against a small synthetic package that breaks it, and
the real integration is checked to pass.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _audit():
    spec = importlib.util.spec_from_file_location("_nova_audit", ROOT / "scripts" / "audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path, files: dict) -> tuple:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    audit = _audit()
    audit._PARSE_CACHE.clear()
    return audit, [p for p in tmp_path.rglob("*.py")]


def _facades_present(files: dict) -> dict:
    """The real facade list, as empty stubs, so only the rule under test fires."""
    audit = _audit()
    return {**{f"{name}.py": "" for name in audit.FACADE_LIMITS}, **files}


def test_real_integration_passes():
    audit = _audit()
    root = ROOT / "custom_components" / "nova"
    assert audit.architecture_gate(root, audit._modules(root)) == []


def test_import_time_cycle_fails(tmp_path):
    audit, files = _tree(tmp_path, _facades_present({
        "__init__.py": "",
        "a.py": "from .b import x\ny = 1\n",
        "b.py": "from .a import y\nx = 1\n",
    }))
    problems = audit.architecture_gate(tmp_path, files)
    assert any("import-time cycle" in p and "a" in p and "b" in p for p in problems)


def test_lazy_import_is_not_a_cycle(tmp_path):
    audit, files = _tree(tmp_path, _facades_present({
        "__init__.py": "",
        "a.py": "from .b import x\ny = 1\n",
        "b.py": "x = 1\ndef f():\n    from .a import y\n    return y\n",
    }))
    assert not any("cycle" in p for p in audit.architecture_gate(tmp_path, files))


def test_submodule_import_through_package_is_not_a_cycle(tmp_path):
    audit, files = _tree(tmp_path, _facades_present({
        "__init__.py": "from . import a\n",
        "a.py": "from . import b\n",
        "b.py": "x = 1\n",
    }))
    assert not any("cycle" in p for p in audit.architecture_gate(tmp_path, files))


def test_disallowed_package_dependency_fails(tmp_path):
    audit, files = _tree(tmp_path, _facades_present({
        "__init__.py": "",
        "providers/__init__.py": "",
        "providers/x.py": "def f():\n    from ..agent_runtime import y\n",
        "agent_runtime/__init__.py": "",
        "agent_runtime/y.py": "",
    }))
    problems = audit.architecture_gate(tmp_path, files)
    assert any("providers may not import agent_runtime" in p for p in problems)


def test_leaf_package_importing_core_fails(tmp_path):
    audit, files = _tree(tmp_path, _facades_present({
        "__init__.py": "",
        "nova_config.py": "",
        "persistence/__init__.py": "",
        "persistence/x.py": "from .. import nova_config\n",
    }))
    problems = audit.architecture_gate(tmp_path, files)
    assert any("leaf package persistence imports nova_config" in p for p in problems)


def test_facade_growth_fails(tmp_path):
    audit, files = _tree(tmp_path, _facades_present({
        "__init__.py": "",
        "automation_creator.py": "def new_logic():\n    return 1\n",
        "pattern_analyzer.py": "x = 1\n" * 200,
    }))
    problems = audit.architecture_gate(tmp_path, files)
    assert any("automation_creator.py gained definitions: ['new_logic']" in p for p in problems)
    assert any("pattern_analyzer.py grew to 200 lines" in p for p in problems)
