"""Repro probe: does a SIGKILLed Appium wedge at backoff(port_conflict)? (2026-07-29)

The defect chain under test (spec 2026-07-27):
  D1 the agent's node loop cancels a mid-spawn auto-restart task and leaks its
     Appium child; D2 nothing reclaims the port that child still holds; so every
     later start raises PortOccupiedError and the backend ladder escalates.

Protocol (detect -> print -> never silently clean):
  per trial: baseline available -> read (pid, port) -> kill -9 -> poll for
  (a) recovery: a NEW pid on the device, state back to available
  (b) the wedge: >1 live `appium server --port <port>` process, or a backend
      lifecycle summary of backoff/suppressed, or review_required
  Each trial prints a timeline. Trials stop early on the first wedge.

The race needs the kill to land while an auto-restart is mid-spawn, so a single
trial proves nothing: --trials defaults to 6 (the diagnosis reproduced in two).

Run:  cd testkit && STATETEST_DEVICE_TARGET=<t> \
        uv run --extra dev python ../scripts/state-testing/exp_port_conflict_wedge.py --trials 6
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone

from baseline import reset_to_available
from config import load_config
from gridfleet_testkit import GridFleetClient
from observe import Observer
from session import resolve_device_id
from triggers import Triggers


def say(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def live_appium_pids(port: int) -> list[int]:
    """Every live `appium server --port <port>` process on this host."""
    proc = subprocess.run(
        ["pgrep", "-f", f"appium server --port {port}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return [int(tok) for tok in proc.stdout.split() if tok.strip().isdigit()]


def trial(cfg, obs: Observer, trig: Triggers, device_id: str, index: int, watch: float) -> bool:
    """One kill trial. Returns True when the wedge reproduced."""
    reset_to_available(cfg, device_id)
    node = trig.appium_node_info(device_id)
    pid_before = node.get("pid")
    port = node.get("port") or cfg.appium_port
    say(f"trial {index}: pid_before={pid_before} port={port} live_pids={live_appium_pids(port)}")
    trig.kill_appium_pid(device_id)

    started = time.monotonic()
    while time.monotonic() - started < watch:
        time.sleep(2.0)
        state = obs.device(device_id)
        pids = live_appium_pids(port)
        pid_now = trig.appium_node_info(device_id).get("pid")
        say(
            f"  t+{time.monotonic() - started:5.1f}s state={state.operational_state} "
            f"summary={state.lifecycle_summary_state} review={state.review_required} "
            f"pid_now={pid_now} live_pids={pids}"
        )
        if len(pids) > 1:
            say(f"  WEDGE: {len(pids)} appium processes hold port {port} — the leak reproduced")
            return True
        if state.review_required or state.lifecycle_summary_state in {"backoff", "suppressed"}:
            say(f"  WEDGE: backend shelved the device (summary={state.lifecycle_summary_state})")
            return True
        if pid_now is not None and pid_now != pid_before and state.operational_state == "available":
            say(f"  recovered: new pid {pid_now}")
            return False
    say("  INCONCLUSIVE: neither recovery nor wedge within the watch window")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--watch", type=float, default=120.0, help="per-trial observation seconds")
    args = ap.parse_args()

    cfg = load_config()
    cfg.assert_local()
    obs, trig = Observer(cfg), Triggers(cfg)
    device_id = resolve_device_id(GridFleetClient(), cfg.device_target)
    say(f"device {device_id} target={cfg.device_target}")
    try:
        for index in range(1, args.trials + 1):
            if trial(cfg, obs, trig, device_id, index, args.watch):
                say(f"reproduced on trial {index}/{args.trials}")
                return 0
        say(f"NOT reproduced in {args.trials} trials — record this before proceeding")
        return 1
    finally:
        reset_to_available(cfg, device_id)


if __name__ == "__main__":
    sys.exit(main())
