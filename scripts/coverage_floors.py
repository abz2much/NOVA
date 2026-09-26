#!/usr/bin/env python3
"""Fail when an architecture package's line coverage drops below its floor.

  python -m pytest tests/ -q --cov=custom_components/nova --cov-report=json:coverage.json
  python scripts/coverage_floors.py coverage.json

Floors sit a few points under each package's coverage when it was set, so
they catch a real drop (a package losing its tests) without failing on
normal churn. There is deliberately no whole-integration target: the older
top-level modules are tested through extracted functions that coverage
cannot attribute, so a global percentage would measure the test style, not
the code. Raise a floor when a package's tests improve; lower one only with
a stated reason in the same change.
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
    "agent_runtime": 71,
    "intent": 33,
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
    failed = False
    for package, floor in sorted(FLOORS.items()):
        covered, statements = totals.get(package, [0, 0])
        pct = 100.0 * covered / statements if statements else 0.0
        ok = pct >= floor
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {package:14s} {pct:5.1f}%  (floor {floor}%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
