"""Print the exact requirements of Home Assistant's own components that
Nova's integration tests load, read from the installed homeassistant
package, one per line for `pip install -r /dev/stdin`.

CI installs whatever Home Assistant the test package brings (or a beta on
top), so these versions follow it instead of being pinned by hand.
"""
from __future__ import annotations

import json
import pathlib
import sys

COMPONENTS = ("conversation", "assist_pipeline", "stt", "tts", "wake_word", "camera", "frontend",
              "ffmpeg")


def requirements(components=COMPONENTS) -> list[str]:
    import homeassistant.components as comps

    root = pathlib.Path(comps.__file__).parent
    found: list[str] = []
    for name in components:
        manifest = root / name / "manifest.json"
        if manifest.exists():
            for req in json.loads(manifest.read_text(encoding="utf-8")).get("requirements", []):
                if req not in found:
                    found.append(req)
    return found


if __name__ == "__main__":
    sys.stdout.write("\n".join(requirements(sys.argv[1:] or COMPONENTS)) + "\n")
