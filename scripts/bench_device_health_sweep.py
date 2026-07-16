#!/usr/bin/env python3
"""Sweep the device-health fold benchmark matrix and consolidate one summary.

Runs each cell as its own pytest process (clean DB fixtures per cell), collects
the FOLD_BENCH_JSON documents, and renders <out>/summary.md. Stdlib only.

Usage:
    python3 scripts/bench_device_health_sweep.py [--only PATTERN] [--iters N] [--out DIR] [--explain]

Requires: the docker compose postgres service up, and backend/.venv synced
(cd backend && uv sync --extra dev).
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"

# name -> env overrides. Scaling axis (sizes x churn), scenario axis at 50, one shape cell.
CELLS: dict[str, dict[str, str]] = {
    "scale-010-healthy": {"FOLD_BENCH_DEVICES": "10"},
    "scale-050-healthy": {"FOLD_BENCH_DEVICES": "50"},
    "scale-150-healthy": {"FOLD_BENCH_DEVICES": "150"},
    "scale-010-churn30": {"FOLD_BENCH_DEVICES": "10", "FOLD_BENCH_CHURN": "0.3"},
    "scale-050-churn30": {"FOLD_BENCH_DEVICES": "50", "FOLD_BENCH_CHURN": "0.3"},
    "scale-150-churn30": {"FOLD_BENCH_DEVICES": "150", "FOLD_BENCH_CHURN": "0.3"},
    "scen-stale-ladder": {"FOLD_BENCH_SCENARIO": "stale-ladder"},
    "scen-stale-run-exclusion": {"FOLD_BENCH_SCENARIO": "stale-run-exclusion"},
    "scen-sparse-unhealthy": {"FOLD_BENCH_SCENARIO": "sparse-unhealthy"},
    "scen-all-unhealthy": {"FOLD_BENCH_SCENARIO": "all-unhealthy"},
    "scen-repeat-unhealthy": {"FOLD_BENCH_SCENARIO": "repeat-unhealthy"},
    "scen-active-claims": {"FOLD_BENCH_SCENARIO": "active-claims"},
    "scen-terminal-noop": {"FOLD_BENCH_SCENARIO": "terminal-noop"},
    "scen-deep-history": {"FOLD_BENCH_SCENARIO": "deep-history"},
    "shape-homogeneous-050": {"FOLD_BENCH_FLEET": "homogeneous"},
}

PYTEST_CMD = [
    "uv",
    "run",
    "pytest",
    "-s",
    "-p",
    "no:randomly",
    "tests/test_bench_folds.py::test_bench_device_health_loop_fold",
    "-o",
    "addopts=",
]


def run_cell(name: str, overrides: dict[str, str], out_dir: Path, iters: str | None, explain: bool) -> bool:
    json_path = out_dir / f"{name}.json"
    # Strip ambient FOLD_BENCH_* knobs: a stray exported var must not silently
    # skew cells that don't override that key — each cell fully owns its knobs.
    env = {k: v for k, v in os.environ.items() if not k.startswith("FOLD_BENCH")}
    env.update({"FOLD_BENCH": "1", "FOLD_BENCH_JSON": str(json_path)})
    env.update(overrides)
    if iters is not None:
        env["FOLD_BENCH_ITERS"] = iters
    if explain:
        env["FOLD_BENCH_EXPLAIN"] = "1"
    print(f"=== {name} ===", flush=True)
    result = subprocess.run(PYTEST_CMD, cwd=BACKEND, env=env, check=False)
    ok = result.returncode == 0 and json_path.exists()
    if not ok:
        print(f"!!! cell {name} FAILED (exit {result.returncode})", flush=True)
    return ok


def render_summary(out_dir: Path, statuses: dict[str, bool]) -> str:
    lines = [
        f"# Device health fold benchmark sweep — {datetime.date.today().isoformat()}",
        "",
        "| Cell | Scenario | Devices | Churn | Fleet | Source q/fold | q/device | Deferred q | Source commits |"
        " Wall median ms | Wall p95 ms |",
        "|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in CELLS:
        if name not in statuses:
            continue  # filtered out by --only
        if not statuses[name]:
            lines.append(f"| {name} | FAILED | | | | | | | | | |")
            continue
        doc = json.loads((out_dir / f"{name}.json").read_text())
        cfg, q, c, wall = doc["config"], doc["queries"], doc["commits"], doc["wall_ms"]["fold_return"]
        devices = int(cfg["devices"])
        lines.append(
            f"| {name} | {cfg['scenario']} | {devices} | {cfg['churn']} | {cfg['fleet']} "
            f"| {q['source_per_fold']:.0f} | {q['source_per_fold'] / devices:.2f} | {q['deferred_per_fold']:.0f} "
            f"| {c['source_per_fold']:.1f} | {wall['median']:.1f} | {wall['p95']:.1f} |"
        )
    failed = sorted(name for name, ok in statuses.items() if not ok)
    if failed:
        lines += ["", f"**FAILED cells ({len(failed)}):** {', '.join(failed)}"]
    lines += [
        "",
        "Lock-wait is deliberately not measured: this is a single-session microbenchmark with no",
        "concurrent writers; contention needs a separate concurrency benchmark.",
        "",
        "Per-cell call-site timing and query plans: see the JSON files next to this summary.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="*", help="fnmatch pattern of cell names to run (default: all)")
    parser.add_argument("--iters", default=None, help="override FOLD_BENCH_ITERS for every cell")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / ".superpowers" / "bench-results" / datetime.date.today().isoformat()),
        help="output directory (default: .superpowers/bench-results/<date>, local-only)",
    )
    parser.add_argument("--explain", action="store_true", help="set FOLD_BENCH_EXPLAIN=1 for every cell")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    selected = {name: env for name, env in CELLS.items() if fnmatch.fnmatch(name, args.only)}
    if not selected:
        print(f"no cells match --only {args.only!r}; known: {', '.join(CELLS)}")
        return 1

    statuses = {name: run_cell(name, env, out_dir, args.iters, args.explain) for name, env in selected.items()}
    summary = render_summary(out_dir, statuses)
    (out_dir / "summary.md").write_text(summary)
    print(summary)
    print(f"results in {out_dir}")
    return 0 if all(statuses.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
