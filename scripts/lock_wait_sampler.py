#!/usr/bin/env python3
"""Sample PostgreSQL row-lock contention on devices/appium_nodes while the lab runs.

Promoted from the throwaway ``.superpowers/loadtest/lock_sampler.py`` into a
maintained tool. Three observation layers (detect -> print; this script never
changes any state):

  1. Lock waits >deadlock_timeout: tails ``docker logs`` of the postgres container
     for "still waiting" lines. Requires log_lock_waits=on + deadlock_timeout=100ms;
     the script checks both at startup and prints the enable command if off.
  2. Sub-100ms waits: samples ``pg_stat_activity`` + ``pg_blocking_pids()`` every
     ``--interval-ms``, classifying blocked/blocking queries by table and tracking
     wait-episode streaks (consecutive samples a pid stays blocked).
  3. Allocation shortfalls: tails the backend container logs for the terminal
     "Not enough devices for requirement" error. NOTE: intermediate SKIP LOCKED
     shortfall retries in create_run are silent (app/runs/service_allocator.py's
     retry loop has no log line), so only allocations that exhausted every retry
     show up here. Count create-run failures from ``run_allocation_churn.py`` for
     retry rates.

Verdict guide (from the 2026-06-10 refactor audit, "schema split" gate):
  - occupancy ~0% AND zero "still waiting" on devices/appium_nodes AND zero
    shortfalls  -> no contention; skip the desired/observed table split.
  - waits present -> check the printed (blocked x blocking) pairs: loop-vs-
    allocation supports the split; loop-vs-loop does not.
  - no waits but shortfalls present -> contention hidden by SKIP LOCKED.

Run from ``backend/`` against the live lab DB (Ctrl-C stops early and still
prints/writes the summary):

    uv run python ../scripts/lock_wait_sampler.py --duration 300 --interval-ms 50 \
        --output-dir ../.superpowers/bench-results/lock-waits/phase10-final

``--output-dir`` receives ``locks.csv`` (every sampled wait) and ``summary.json``
(the printed counters). It defaults to a timestamped directory below
``.superpowers/bench-results/lock-waits/``; passing it explicitly lets one paired
sampler+churn run share a single evidence directory. Output is local-only and
never committed.

Stdlib only apart from ``asyncpg``, which is already a backend dependency and is
imported lazily so ``--help`` works from any environment.
"""

# ruff: noqa: UP017, UP041
# ^ pinned to the Python floor below, file-wide rather than per line: UP017
#   rewrites `timezone.utc` to `datetime.UTC` (3.11+) and UP041 rewrites
#   `asyncio.TimeoutError` to the builtin (only the same object on 3.11+).
#   Both are wrong here. A per-line noqa drifts as lines move.

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import re
import signal
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
# Held to the same floor as its paired driver (run_allocation_churn.py, run from
# testkit/ on 3.10) so the two tools can never diverge on interpreter. No 3.11+
# construct: ``datetime.UTC``, the unified builtin ``TimeoutError`` (on 3.10
# ``asyncio.wait_for`` raises ``asyncio.TimeoutError``, which is NOT the
# builtin), ``tomllib``, ``except*``/``TaskGroup``, ``Self``, ``StrEnum``.
PYTHON_FLOOR = (3, 10)

TABLES_OF_INTEREST = ("devices", "appium_nodes", "device_reservations", "sessions", "grid_session_queue")

SAMPLE_SQL = """
SELECT a.pid,
       a.wait_event_type,
       a.wait_event,
       left(coalesce(a.query, ''), 200)  AS blocked_query,
       b.pid                              AS blocking_pid,
       left(coalesce(b.query, ''), 200)  AS blocking_query,
       b.state                            AS blocking_state
FROM pg_stat_activity a
JOIN LATERAL unnest(pg_blocking_pids(a.pid)) bp(pid) ON true
JOIN pg_stat_activity b ON b.pid = bp.pid
WHERE a.wait_event_type = 'Lock'
"""

CSV_HEADER = [
    "ts",
    "blocked_pid",
    "wait_event",
    "blocked_kind",
    "blocking_kind",
    "blocking_state",
    "blocked_query",
    "blocking_query",
]


def default_output_dir() -> Path:
    """Timestamped local-only directory; ``--output-dir`` overrides it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return REPO_ROOT / ".superpowers" / "bench-results" / "lock-waits" / stamp


def classify(query: str) -> str:
    """Name every table of interest the statement touches, in table order.

    Word-boundary matched, so ``devices_backup`` is not ``devices``.
    """
    lowered = query.lower()
    hits = [table for table in TABLES_OF_INTEREST if re.search(rf"\b{table}\b", lowered)]
    return "+".join(hits) if hits else "other"


class Sampler:
    def __init__(self, dsn: str, interval_ms: int, csv_path: Path) -> None:
        self.dsn = dsn
        self.interval = interval_ms / 1000.0
        self.csv_path = csv_path
        self.samples_total = 0
        self.samples_with_wait = 0
        self.blocked_by_kind: Counter[str] = Counter()
        self.pair_counts: Counter[tuple[str, str]] = Counter()
        self.episodes: list[tuple[int, str, str]] = []  # (streak_len, blocked_kind, blocking_kind)
        self._streaks: dict[int, tuple[int, str, str]] = {}  # pid -> (len, blocked_kind, blocking_kind)

    async def run(self, stop: asyncio.Event) -> None:
        import asyncpg  # noqa: PLC0415 — live-only, so --help works from any venv

        conn = await asyncpg.connect(self.dsn)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.csv_path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CSV_HEADER)
                while not stop.is_set():
                    rows = await conn.fetch(SAMPLE_SQL)
                    self.samples_total += 1
                    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                    waiting_pids: set[int] = set()
                    if rows:
                        self.samples_with_wait += 1
                    for row in rows:
                        blocked_kind = classify(row["blocked_query"])
                        blocking_kind = classify(row["blocking_query"])
                        waiting_pids.add(row["pid"])
                        self.blocked_by_kind[blocked_kind] += 1
                        self.pair_counts[(blocked_kind, blocking_kind)] += 1
                        previous = self._streaks.get(row["pid"])
                        self._streaks[row["pid"]] = (
                            (previous[0] if previous else 0) + 1,
                            blocked_kind,
                            blocking_kind,
                        )
                        writer.writerow(
                            [
                                now,
                                row["pid"],
                                row["wait_event"],
                                blocked_kind,
                                blocking_kind,
                                row["blocking_state"],
                                row["blocked_query"],
                                row["blocking_query"],
                            ]
                        )
                        print(
                            f"[{now}] LOCK-WAIT pid={row['pid']} {row['wait_event']} "
                            f"blocked[{blocked_kind}] <- blocking[{blocking_kind}]"
                        )
                    for pid in [pid for pid in self._streaks if pid not in waiting_pids]:
                        self.episodes.append(self._streaks.pop(pid))
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=self.interval)
        finally:
            self.episodes.extend(self._streaks.values())
            await conn.close()


async def tail_container(
    name: str,
    patterns: list[str],
    counter: Counter[str],
    stop: asyncio.Event,
    context_lines: int,
) -> None:
    """Tail ``docker logs -f`` and print/count lines matching any pattern."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "logs",
        "-f",
        "--since",
        "0s",
        "--tail",
        "0",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    echo_remaining = 0
    try:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            matched = next((pattern for pattern in patterns if pattern in line), None)
            if matched:
                counter[matched] += 1
                echo_remaining = context_lines
            if echo_remaining > 0:
                print(f"[{name}] {line}")
                echo_remaining -= 1
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await proc.wait()


async def check_pg_settings(dsn: str) -> None:
    import asyncpg  # noqa: PLC0415 — live-only, so --help works from any venv

    conn = await asyncpg.connect(dsn)
    try:
        log_lock_waits = await conn.fetchval("SHOW log_lock_waits")
        deadlock_timeout = await conn.fetchval("SHOW deadlock_timeout")
    finally:
        await conn.close()
    print(f"postgres settings: log_lock_waits={log_lock_waits}  deadlock_timeout={deadlock_timeout}")
    if log_lock_waits != "on" or deadlock_timeout != "100ms":
        print(
            "  WARNING: layer-1 (>100ms wait logging) not fully enabled. To enable:\n"
            "    docker compose exec postgres psql -U gridfleet -c \\\n"
            '      "ALTER SYSTEM SET log_lock_waits = on; '
            "ALTER SYSTEM SET deadlock_timeout = '100ms'; SELECT pg_reload_conf();\""
        )


def build_summary(
    *,
    sampler: Sampler,
    pg_hits: Counter[str],
    backend_hits: Counter[str],
    interval_ms: int,
) -> dict[str, Any]:
    """The printed counters as one JSON-serialisable document."""
    occupancy = (sampler.samples_with_wait / sampler.samples_total * 100) if sampler.samples_total else 0.0
    lengths = sorted(episode[0] for episode in sampler.episodes)
    longest = max(sampler.episodes, key=lambda episode: episode[0]) if sampler.episodes else None
    return {
        "samples": sampler.samples_total,
        "samples_with_wait": sampler.samples_with_wait,
        "occupancy_pct": occupancy,
        "interval_ms": interval_ms,
        "blocked_by_kind": dict(sampler.blocked_by_kind.most_common()),
        "top_pairs": [
            {"blocked": blocked, "blocking": blocking, "samples": count}
            for (blocked, blocking), count in sampler.pair_counts.most_common(10)
        ],
        "wait_episodes": {
            "count": len(lengths),
            "median_ms": lengths[len(lengths) // 2] * interval_ms if lengths else 0,
            "max_ms": longest[0] * interval_ms if longest else 0,
            "longest": {"blocked": longest[1], "blocking": longest[2]} if longest else None,
        },
        "postgres_still_waiting": sum(pg_hits.values()),
        "terminal_allocation_shortfalls": sum(backend_hits.values()),
    }


def print_summary(summary: dict[str, Any]) -> None:
    interval_ms = summary["interval_ms"]
    print("\n" + "=" * 72)
    print("LOCK SAMPLER SUMMARY")
    print("=" * 72)
    print(
        f"samples: {summary['samples']}  with >=1 lock wait: {summary['samples_with_wait']}  "
        f"occupancy: {summary['occupancy_pct']:.2f}%"
    )

    print(f"\nblocked sample counts by table (1 count ~= {interval_ms}ms of observed waiting):")
    for kind, count in summary["blocked_by_kind"].items() or [("(none)", 0)]:
        print(f"  {kind:30s} {count:6d}  (~{count * interval_ms / 1000:.1f}s)")

    print("\ntop (blocked <- blocking) pairs:")
    for pair in summary["top_pairs"] or [{"blocked": "(none)", "blocking": "", "samples": 0}]:
        print(f"  {pair['blocked']:25s} <- {pair['blocking']:25s} {pair['samples']:6d}")

    episodes = summary["wait_episodes"]
    if episodes["count"]:
        longest = episodes["longest"]
        print(
            f"\nwait episodes: {episodes['count']}  median ~{episodes['median_ms']}ms  "
            f"max ~{episodes['max_ms']}ms ({longest['blocked']} <- {longest['blocking']})"
        )
    else:
        print("\nwait episodes: 0")

    print(f"\nlayer-1 postgres 'still waiting' (>deadlock_timeout) hits: {summary['postgres_still_waiting']}")
    print(f"terminal allocation shortfalls ('Not enough devices'): {summary['terminal_allocation_shortfalls']}")
    print(
        "\nverdict guide: occupancy ~0 + zero still-waiting on devices/appium_nodes\n"
        "+ zero shortfalls => no contention, skip the desired/observed table split.\n"
        "Waits present => judge by the pairs above (loop-vs-allocation supports the\n"
        "split; loop-vs-loop does not). No waits but shortfalls => SKIP LOCKED is\n"
        "masking contention."
    )


def write_json_atomically(path: Path, document: dict[str, Any]) -> None:
    """Write *document* so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(document, indent=2) + "\n")
    os.replace(temp, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=0, help="seconds to run (0 = until Ctrl-C)")
    parser.add_argument("--interval-ms", type=int, default=100, help="sampling interval (default 100)")
    parser.add_argument("--dsn", default="postgresql://gridfleet:gridfleet@localhost:5432/gridfleet")
    parser.add_argument("--pg-container", default="docker-postgres-1")
    parser.add_argument("--backend-container", default="docker-backend-1")
    parser.add_argument("--no-docker-logs", action="store_true", help="skip container log tailing (layers 1+3)")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="directory for locks.csv and summary.json (default: a timestamped dir under "
        ".superpowers/bench-results/lock-waits/)",
    )
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    await check_pg_settings(args.dsn)

    csv_path = output_dir / "locks.csv"
    sampler = Sampler(args.dsn, args.interval_ms, csv_path)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.duration > 0:
        loop.call_later(args.duration, stop.set)

    pg_hits: Counter[str] = Counter()
    backend_hits: Counter[str] = Counter()
    tasks = [asyncio.create_task(sampler.run(stop))]
    if not args.no_docker_logs:
        tasks.append(
            asyncio.create_task(
                tail_container(args.pg_container, ["still waiting", "deadlock detected"], pg_hits, stop, 5)
            )
        )
        tasks.append(
            asyncio.create_task(
                tail_container(args.backend_container, ["Not enough devices for requirement"], backend_hits, stop, 1)
            )
        )

    print(
        f"sampling every {args.interval_ms}ms"
        + (f" for {args.duration}s" if args.duration else " until Ctrl-C")
        + f"; evidence -> {output_dir}"
    )
    await asyncio.gather(*tasks, return_exceptions=True)
    summary = build_summary(sampler=sampler, pg_hits=pg_hits, backend_hits=backend_hits, interval_ms=args.interval_ms)
    print_summary(summary)
    write_json_atomically(output_dir / "summary.json", summary)
    print(f"\nwrote {csv_path} and {output_dir / 'summary.json'}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
