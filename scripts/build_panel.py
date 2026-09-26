#!/usr/bin/env python3
"""Build custom_components/nova/frontend/nova-panel.js from frontend/src/.

The panel ships as one plain script with no runtime dependencies; HACS
installs it as is. Its source lives in frontend/src/ as ordered parts:
the NOVA3D engine, then the NovaPanel class split by area. The build joins
them in SOURCES order behind a generated banner. It is pure concatenation:
no transform, no timestamps, no source maps and no local paths, so the
same sources always give the same bytes.

  python scripts/build_panel.py           write nova-panel.js
  python scripts/build_panel.py --check   exit 1 if nova-panel.js is stale

Panel parts other than nova3d.js are fragments of one class body and are
not valid JavaScript on their own; `node --check` runs on the built file.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
OUT = ROOT / "custom_components" / "nova" / "frontend" / "nova-panel.js"

SOURCES = (
    "nova3d.js",                     # NOVA3D engine, also loaded by frontend/dev
    "panel/core.js",                 # class opening, lifecycle, HA contract, data
    "panel/render.js",               # tab shell and dashboard
    "panel/logs.js",                 # system log, spoken history, actions, decisions
    "panel/memory.js",
    "panel/intrusion.js",
    "panel/suggestions.js",          # suggestions and automation probation
    "panel/settings-catalogue.js",   # settings groups and cards
    "panel/residence.js",            # Residence 3D tab
    "panel/settings-cards.js",
    "panel/settings-models.js",      # AI model roles and credentials wiring
    "panel/settings-cards-more.js",
    "panel/live-data.js",            # live data patching, cameras, area tiles
    "panel/wiring.js",               # event wiring, settings save
    "panel/core-animation.js",       # stellar core canvas
    "panel/floor-plan.js",
    "panel/floor-plan-geometry.js",  # coverage and line-of-sight maths
    "panel/floor-plan-editor.js",
    "panel/styles.js",
    "panel/define.js",               # class closing and custom element registration
)

BANNER = (
    "/* GENERATED FILE: do not edit. Built by scripts/build_panel.py from\n"
    " * frontend/src/. Edit the sources there, then run the build. */\n"
)


def build() -> str:
    parts = [BANNER]
    for rel in SOURCES:
        text = (SRC / rel).read_text(encoding="utf-8")
        if "\x00" in text or "\r" in text:
            raise SystemExit(f"build_panel: {rel} contains a NUL or CR byte")
        if not text.endswith("\n"):
            raise SystemExit(f"build_panel: {rel} must end with a newline")
        parts.append(text)
    listed = {SRC / rel for rel in SOURCES}
    unlisted = sorted(str(p.relative_to(SRC)) for p in SRC.rglob("*.js") if p not in listed)
    if unlisted:
        raise SystemExit(f"build_panel: source files not in SOURCES: {unlisted}")
    return "".join(parts)


def main(argv: list[str]) -> int:
    text = build()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("nova-panel.js is out of date: run python scripts/build_panel.py",
                  file=sys.stderr)
            return 1
        print("nova-panel.js is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
