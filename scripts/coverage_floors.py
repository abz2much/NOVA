#!/usr/bin/env python3
"""Fail when a package's, or one named file's, line coverage drops below its floor.

  python -m pytest tests/ -q --cov=custom_components/nova --cov-report=json:coverage.json
  python scripts/coverage_floors.py coverage.json

Floors sit a few points under each package's coverage when it was set, so
they catch a real drop (a package losing its tests) without failing on
normal churn. FILE_FLOORS does the same for single top level files, for a
file that holds safety code but is not a package (the key is its path under
custom_components/nova/). There is deliberately no whole-integration target:
the older top-level modules are tested through extracted functions that
coverage cannot attribute, so a global percentage would measure the test
style, not the code. Raise a floor when a package's tests improve; lower one
only with a stated reason in the same change.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

FLOORS = {
    "persistence": 92,
    "audio": 91,
    "cognitive": 90,
    "vision": 90,
    "providers": 86,
    "automation": 81,
    "diagnostics": 77,
    "agent_runtime": 74,
    "intent": 35,
}

# Per file floors, for top level files that are not part of a package.
# cognitive_core.py (intrusion, lockdown, freeze, the proactive manager and the
# main tick) was at 63.1% before its safety code was pinned by tests (8.7.15)
# and measured 97.5% after (the CI command, python -m pytest tests/), so the
# floor sits three and a half points under that: the same margin the package
# floors use, low enough for normal churn, high enough that deleting the
# tests, or the safety branches they pin, fails the build.
#
# 8.7.17 split that code into core_*.py with cognitive_core.py kept as the
# facade. The one 94% floor became a floor per file, each set the same three
# and a half points under that file's measured coverage (rounded down). The
# files together measured 97.5% (2463 of 2526 lines), as before the split,
# and each file is now held on its own, so no moved code has less protection.
#
# 8.7.19 pinned the agent's riskiest decision code the same way. Floors for
# those six files sit three and a half points under their measured coverage,
# rounded down (measured in the comments; before 8.7.19 in brackets).
FILE_FLOORS = {
    "intent/intent_router.py": 83,                      # 87.2% (30.8%)
    "agent_runtime/capabilities/safety_modes.py": 83,   # 87.2% (59.0%)
    "agent_runtime/capabilities/control.py": 88,        # 92.1% (89.1%)
    "ws_modes.py": 77,                                  # 80.9% (0.0%)
    "agent_runtime/loop.py": 75,                        # 78.8% (67.8%)
    "agent_runtime/capabilities/memory.py": 70,         # 73.5% (69.4%)
    "cognitive_core.py": 90,       # 94.3%: the facade (__delattr__ is unused)
    "core_autonomy.py": 96,        # 100.0%
    "core_bridge.py": 96,          # 100.0%
    "core_common.py": 96,          # 100.0%
    "core_delivery.py": 92,        # 95.9%
    "core_ignore.py": 96,          # 100.0%
    "core_learning.py": 89,        # 92.7%
    "core_lockdown.py": 94,        # 98.0%
    "core_lockdown_sync.py": 96,   # 100.0%
    "core_pattern_store.py": 94,   # 97.6%
    "core_proactive.py": 93,       # 97.2%
    "core_runtime.py": 93,         # 96.8%
    "core_safety.py": 95,          # 98.5%
    "core_state.py": 96,           # 100.0%
}


def main(argv: list[str]) -> int:
    path = argv[0] if argv else "coverage.json"
    data = json.load(open(path, encoding="utf-8"))
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for name, info in data["files"].items():
        rel = name.replace("\\", "/").split("custom_components/nova/")[-1]
        package = rel.split("/")[0] if "/" in rel else ""
        if package in FLOORS:
            totals[package][0] += info["summary"]["covered_lines"]
            totals[package][1] += info["summary"]["num_statements"]
    file_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for name, info in data["files"].items():
        rel = name.replace("\\", "/").split("custom_components/nova/")[-1]
        if rel in FILE_FLOORS:
            file_totals[rel][0] += info["summary"]["covered_lines"]
            file_totals[rel][1] += info["summary"]["num_statements"]
    failed = False
    for package, floor in sorted(FLOORS.items()):
        covered, statements = totals.get(package, [0, 0])
        pct = 100.0 * covered / statements if statements else 0.0
        ok = pct >= floor
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {package:14s} {pct:5.1f}%  (floor {floor}%)")
    for rel, floor in sorted(FILE_FLOORS.items()):
        covered, statements = file_totals.get(rel, [0, 0])
        pct = 100.0 * covered / statements if statements else 0.0
        ok = pct >= floor
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {rel:18s} {pct:5.1f}%  (floor {floor}%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
