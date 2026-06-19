"""Run heartbeat thread and exit/signal cleanup for GridFleet test runs."""

from __future__ import annotations

import atexit
import logging
import signal
import threading
from typing import TYPE_CHECKING, Literal, cast

import httpx2 as httpx

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from .client import GridFleetClient

    RunCleanup = Callable[[], None]

logger = logging.getLogger("gridfleet_testkit")

RunCleanupPolicy = Literal["complete", "cancel", "noop"]


class HeartbeatThread(threading.Thread):
    """Background thread that sends periodic heartbeat pings for an active test run."""

    def __init__(
        self,
        base_url: str,
        run_id: str,
        interval: int = 30,
        auth: httpx.BasicAuth | None = None,
    ):
        super().__init__(daemon=True)
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.interval = interval
        self._auth = auth
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                resp = httpx.post(
                    f"{self.base_url}/runs/{self.run_id}/heartbeat",
                    timeout=10,
                    auth=self._auth,
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("state") in ("expired", "cancelled"):
                    logger.warning("Run %s is %s, stopping heartbeat", self.run_id, result["state"])
                    break
            except Exception:
                logger.debug("Heartbeat failed for run %s, will retry", self.run_id)

    def stop(self) -> None:
        self._stop_event.set()


def _apply_run_cleanup_policy(client: GridFleetClient, run_id: str, policy: RunCleanupPolicy) -> None:
    if policy == "complete":
        client.complete_run(run_id)
    elif policy == "cancel":
        client.cancel_run(run_id)


def register_run_cleanup(
    client: GridFleetClient,
    run_id: str,
    heartbeat_thread: HeartbeatThread | None = None,
    *,
    on_exit: RunCleanupPolicy = "noop",
    on_signal: RunCleanupPolicy = "cancel",
    install_signal_handlers: bool = False,
    chain_signals: bool = True,
    join_timeout_sec: float | None = 5.0,
) -> RunCleanup:
    """Register exit cleanup for a run and optionally install signal handlers.

    Normal process exit defaults to ``noop`` because atexit cannot know whether
    the run succeeded. Callers that know outcome should explicitly complete or
    cancel the run, or pass ``on_exit=`` when legacy auto-finalization is wanted.
    """

    called_lock = threading.Lock()
    called = False
    previous_handlers: dict[signal.Signals, signal.Handlers] = {}

    def cleanup(policy: RunCleanupPolicy = on_exit) -> None:
        nonlocal called
        with called_lock:
            if called:
                return
            called = True
        if heartbeat_thread:
            heartbeat_thread.stop()
            heartbeat_thread.join(timeout=join_timeout_sec)
            if heartbeat_thread.is_alive():
                logger.warning("Heartbeat thread for run %s did not stop within %s seconds", run_id, join_timeout_sec)
        try:
            _apply_run_cleanup_policy(client, run_id, policy)
        except Exception:
            logger.warning("Failed to apply %s cleanup policy for run %s", policy, run_id, exc_info=True)

    def signal_cleanup(sig: int, frame: FrameType | None) -> None:
        cleanup(on_signal)
        if not chain_signals:
            return
        previous = previous_handlers.get(signal.Signals(sig))
        if callable(previous):
            previous(sig, frame)
        elif previous is signal.SIG_DFL:
            # Restore default and re-raise so the kernel applies it (e.g. SIGTERM terminates).
            signal.signal(sig, signal.SIG_DFL)
            signal.raise_signal(sig)
        # SIG_IGN: do nothing (intentional drop).

    atexit.register(cleanup)
    if install_signal_handlers:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = cast("signal.Handlers", signal.getsignal(sig))
            signal.signal(sig, signal_cleanup)
    return cleanup
