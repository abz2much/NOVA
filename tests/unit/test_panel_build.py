"""Phase 10: the panel is built from frontend/src/ into one HACS asset.

nova-panel.js stays the single file Home Assistant loads. It must be the
exact output of scripts/build_panel.py, deterministic, free of local paths,
and still one NovaPanel class registered once.
"""
import importlib.util
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "custom_components" / "nova" / "frontend" / "nova-panel.js"


def _build_module():
    spec = importlib.util.spec_from_file_location(
        "_build_panel", ROOT / "scripts" / "build_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_panel_is_the_build_output():
    assert PANEL.read_text(encoding="utf-8") == _build_module().build(), (
        "nova-panel.js is stale: run python scripts/build_panel.py")


def test_build_is_deterministic():
    bp = _build_module()
    assert bp.build() == bp.build()


def test_every_source_file_is_built():
    bp = _build_module()
    on_disk = sorted(str(p.relative_to(bp.SRC)) for p in bp.SRC.rglob("*.js"))
    assert sorted(bp.SOURCES) == on_disk
    assert len(set(bp.SOURCES)) == len(bp.SOURCES)


def test_panel_has_no_control_bytes_or_local_paths():
    raw = PANEL.read_bytes()
    assert b"\x00" not in raw and b"\r" not in raw
    text = raw.decode("utf-8")
    for leak in ("/Users/", "/home/", "C:\\", "sourceMappingURL"):
        assert leak not in text, leak


def test_panel_defines_one_element_and_one_engine():
    text = PANEL.read_text(encoding="utf-8")
    assert text.count('customElements.define("nova-panel", NovaPanel)') == 1
    assert text.count("class NovaPanel extends HTMLElement") == 1
    assert text.count("const NOVA3D = (function () {") == 1
    assert text.count("window.NOVA3D = NOVA3D") == 1


def test_no_panel_method_is_defined_twice():
    """A later duplicate silently replaces an earlier method in a class body,
    so a split or merge mistake would change behaviour without an error."""
    text = PANEL.read_text(encoding="utf-8")
    body = text[text.index("class NovaPanel extends HTMLElement"):]
    names = re.findall(
        r"^  (?:static |async )?((?:get |set )?[A-Za-z_$][\w$]*)\s*\(", body, re.M)
    dupes = sorted(n for n, c in Counter(names).items() if c > 1)
    assert not dupes, dupes
    assert len(names) > 200


def test_version_bump_edits_the_source_and_rebuilds():
    script = (ROOT / "scripts" / "bump_version.sh").read_text(encoding="utf-8")
    assert "frontend/src/panel/core.js" in script
    assert "scripts/build_panel.py" in script
    core = (ROOT / "frontend" / "src" / "panel" / "core.js").read_text(encoding="utf-8")
    version = __import__("json").loads(
        (ROOT / "custom_components" / "nova" / "manifest.json").read_text())["version"]
    assert f" * v{version}\n" in core and f"%c v{version} " in core


def test_shipped_frontend_holds_only_runtime_files():
    shipped = sorted(p.name for p in PANEL.parent.iterdir())
    assert shipped == ["i18n", "nova-logo.png", "nova-panel.js"]
