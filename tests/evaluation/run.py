"""Developer command for Nova's deterministic evaluation.

    python tests/evaluation/run.py                  # human readable report
    python tests/evaluation/run.py --json -         # JSON report on stdout
    python tests/evaluation/run.py --json out.json  # JSON report to a file
    python tests/evaluation/run.py --id VOICE-003 --category critical_gate

Exit status: 0 when every scenario passes or matches its pinned baseline
mismatch, 1 when any safety property is violated, 2 when the harness
itself failed (a harness error is never reported as a Nova result).
Uses only the test suite's own stubs: no Home Assistant, no network, no
provider, no /config.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
# Run as a script, this directory would shadow tests/fakes.py with our own
# fakes.py; import the harness as the `evaluation` package instead.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
sys.path.insert(0, _TESTS)


def build_report(ids=None, categories=None) -> dict:
    from evaluation import harness, metrics, scenarios
    chosen = scenarios.load_scenarios(ids=ids, categories=categories)
    results = harness.run_scenarios(chosen)
    baseline = scenarios.load_baseline()
    for r in results:
        pinned = baseline.get(r["id"], {})
        r["baseline_verdict"] = pinned.get("verdict", "pass")
        if pinned.get("note"):
            r["baseline_note"] = pinned["note"]
    return {
        "summary": metrics.summary(results),
        "metrics": metrics.compute(chosen, results),
        "results": results,
    }


def exit_code(report: dict) -> int:
    s = report["summary"]
    if s["harness_errors"]:
        return 2
    if s["safety_failures"]:
        return 1
    return 0


def _fmt_value(m):
    if m["value"] is None:
        return "n/a (no applicable scenarios)"
    return f"{m['value']:.4f} ({m['numerator']}/{m['denominator']})"


def render_text(report: dict) -> str:
    s = report["summary"]
    lines = ["Nova evaluation", "===============",
             f"scenarios {s['total']}: pass {s['pass']}, mismatch {s['mismatch']}, "
             f"safety failure {s['safety_failure']}, harness error {s['harness_error']}", ""]
    lines.append("Safety")
    lines.append("------")
    if s["safety_failures"]:
        for r in report["results"]:
            if r["verdict"] == "safety_failure":
                for v in r["safety_violations"]:
                    lines.append(f"  FAIL {r['id']} [{v['property']}] {v['detail']}")
    else:
        lines.append("  no safety property violated")
    lines += ["", "Metrics", "-------"]
    for m in report["metrics"]:
        failing = f"  failing: {', '.join(m['failing'])}" if m["failing"] else ""
        lines.append(f"  {m['name']}: {_fmt_value(m)}{failing}")
    lines += ["", "Scenarios", "---------"]
    for r in report["results"]:
        mark = "" if r["verdict"] == r["baseline_verdict"] else "  (differs from baseline)"
        lines.append(f"  {r['verdict']:<14} {r['id']:<9} {r['category']}{mark}")
        for mm in r["mismatches"]:
            lines.append(f"      {mm['field']}: expected {mm['expected']!r}, "
                         f"observed {mm['observed']!r}")
        for e in r["errors"]:
            lines.append(f"      error: {e}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--json", metavar="PATH", help="write the JSON report to PATH ('-' for stdout)")
    p.add_argument("--id", action="append", dest="ids", help="run only this scenario ID")
    p.add_argument("--category", action="append", dest="categories",
                   help="run only this category")
    args = p.parse_args(argv)
    report = build_report(args.ids, args.categories)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json == "-":
        sys.stdout.write(text)
    else:
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(text)
        sys.stdout.write(render_text(report))
    return exit_code(report)


if __name__ == "__main__":
    # Nova logs warnings on the paths under test (a shush, a provider
    # failure); keep them out of the report.
    logging.getLogger().addHandler(logging.NullHandler())
    sys.exit(main())
