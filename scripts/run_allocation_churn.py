#!/usr/bin/env python3
"""Allocation churn driver for the lock-contention measurement (lock_wait_sampler.py).

Promoted from the throwaway ``.superpowers/loadtest/run_churn.py`` into a
maintained tool. Cycles reserve -> hold -> cancel **against a live backend**,
rotating across every distinct ``(pack_id, platform_id)`` so each device row gets
Stage-3 ``SELECT ... FOR UPDATE SKIP LOCKED`` allocation pressure plus the
release-path row locks, while the observation loops run as normal.

Counts client-side 409s ("no allocatable device") as the shortfall proxy — the
backend's intermediate SKIP LOCKED retries are silent, so this is the visible
signal that allocation lost a race.

**This drives a real backend and reserves real devices.** Run it only against a
stack you are allowed to disturb, alongside the sampler:

    cd testkit
    uv run --extra dev python ../scripts/run_allocation_churn.py \
        --duration 280 --hold-sec 0.2 --gap-sec 0.1 \
        --summary-json ../.superpowers/bench-results/lock-waits/phase10-final/churn-summary.json

``--summary-json`` writes the printed counters atomically at exit — on the normal
deadline, on Ctrl-C, and on SIGTERM alike — so a paired sampler run always has
its churn numbers. It defaults to a timestamped path below
``.superpowers/bench-results/lock-waits/``; output is local-only and never
committed.

Stdlib only apart from ``httpx`` and ``gridfleet_testkit``, which are imported
lazily so ``--help`` works from any environment.
"""

# ruff: noqa: UP017
# ^ pinned to the Python floor below, file-wide rather than per line: UP017
#   rewrites `timezone.utc` to `datetime.UTC` (3.11+) and UP041 rewrites
#   `asyncio.TimeoutError` to the builtin (only the same object on 3.11+).
#   Both are wrong here. A per-line noqa drifts as lines move.

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import FrameType, ModuleType

    from gridfleet_testkit import GridFleetClient

REPO_ROOT = Path(__file__).resolve().parent.parent
HTTP_CONFLICT = 409
# The plan runs this from ``testkit/``, whose pyproject floor is >=3.10 and whose
# resolved interpreter is 3.10. Nothing here may use a 3.11+ construct:
# ``datetime.UTC``, the unified builtin ``TimeoutError``, ``tomllib``,
# ``except*``/``TaskGroup``, ``Self``, ``StrEnum``. ``ruff --fix`` will happily
# rewrite the first two into 3.11+ forms; the noqa above and
# tests/test_lock_wait_sampler.py::test_promoted_scripts_hold_the_python_floor
# are what stop that.
PYTHON_FLOOR = (3, 10)


@dataclass(slots=True)
class ChurnCounters:
    cycles: int = 0
    reserved_ok: int = 0
    shortfall_409: int = 0
    errors: int = 0


def default_summary_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return REPO_ROOT / ".superpowers" / "bench-results" / "lock-waits" / stamp / "churn-summary.json"


def write_summary_json(path: Path, counters: ChurnCounters) -> None:
    """Write *counters* so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(asdict(counters), indent=2) + "\n")
    os.replace(temp, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=1450, help="seconds to run")
    parser.add_argument("--hold-sec", type=float, default=1.0, help="reserved hold per cycle")
    parser.add_argument("--gap-sec", type=float, default=0.5, help="pause between cycles")
    parser.add_argument(
        "--summary-json",
        default=None,
        help="path for the exit summary (default: a timestamped churn-summary.json under "
        ".superpowers/bench-results/lock-waits/)",
    )
    return parser.parse_args(argv)


def churnable_combos(devices: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Only combos with a currently-available device.

    A 409 should mean "lost a race", not "that platform has no allocatable
    device to begin with".
    """
    return sorted(
        {
            (device["pack_id"], device["platform_id"])
            for device in devices
            if device.get("pack_id") and device.get("operational_state") == "available"
        }
    )


def _one_cycle(
    *,
    client: GridFleetClient,
    httpx: ModuleType,
    pack_id: str,
    platform_id: str,
    counters: ChurnCounters,
    hold_sec: float,
) -> None:
    run_id = None
    try:
        response = client.reserve_devices(
            name=f"lock-sampler-churn-{counters.cycles}",
            requirements=[{"pack_id": pack_id, "platform_id": platform_id, "count": 1}],
            ttl_minutes=2,
            heartbeat_timeout_sec=60,
        )
        run_id = str(response["id"])
        counters.reserved_ok += 1
        time.sleep(hold_sec)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == HTTP_CONFLICT:
            counters.shortfall_409 += 1
            print(f"[cycle {counters.cycles}] 409 shortfall on ({pack_id}, {platform_id})")
        else:
            counters.errors += 1
            print(f"[cycle {counters.cycles}] HTTP {exc.response.status_code}: {exc}")
    except Exception as exc:  # noqa: BLE001 — keep churning, report at end
        counters.errors += 1
        print(f"[cycle {counters.cycles}] error: {exc}")
    finally:
        if run_id is not None:
            try:
                client.cancel_run(run_id)
            except Exception as exc:  # noqa: BLE001
                counters.errors += 1
                print(f"[cycle {counters.cycles}] cancel failed for {run_id}: {exc}")


def main() -> None:
    # Parsed before the live-only imports so ``--help`` works from any venv.
    args = parse_args()

    import httpx  # noqa: PLC0415 — live-only, so --help works from any venv
    from gridfleet_testkit import GridFleetClient  # noqa: PLC0415 — same

    summary_path = Path(args.summary_json).resolve() if args.summary_json else default_summary_path()

    client = GridFleetClient()
    devices = client.list_devices()
    combos = churnable_combos(devices)
    if not combos:
        raise SystemExit("no available devices found — nothing to churn")
    print(f"devices={len(devices)}  churnable combos={combos}")

    counters = ChurnCounters()
    stopping = False

    def _request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        print("\nstop requested; finishing the current cycle")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _request_stop)

    deadline = time.monotonic() + args.duration
    try:
        while not stopping and time.monotonic() < deadline:
            pack_id, platform_id = combos[counters.cycles % len(combos)]
            counters.cycles += 1
            _one_cycle(
                client=client,
                httpx=httpx,
                pack_id=pack_id,
                platform_id=platform_id,
                counters=counters,
                hold_sec=args.hold_sec,
            )
            if counters.cycles % 50 == 0:
                print(
                    f"[cycle {counters.cycles}] ok={counters.reserved_ok} "
                    f"shortfall_409={counters.shortfall_409} errors={counters.errors}"
                )
            time.sleep(args.gap_sec)
    finally:
        write_summary_json(summary_path, counters)
        print(
            f"\nCHURN SUMMARY: cycles={counters.cycles} reserved_ok={counters.reserved_ok} "
            f"shortfall_409={counters.shortfall_409} errors={counters.errors}"
        )
        print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
