import asyncio
import collections
import contextlib
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import httpx2 as httpx

from agent_app import http_client
from agent_app.appium.exceptions import (
    AlreadyRunningError,
    DeviceNotFoundError,
    InvalidStartPayloadError,
    PortOccupiedError,
    RuntimeMissingError,
    RuntimeNotInstalledError,
    StartDeferredError,
    StartupTimeoutError,
)
from agent_app.appium.log_files import (
    LOG_MAINTENANCE_INTERVAL_SEC,
    appium_log_path,
    open_log_file,
    sweep_log_dir,
    tail_lines,
    truncate_oversized_logs,
)
from agent_app.config import agent_settings
from agent_app.observability import sanitize_log_value
from agent_app.pack.adapter_dispatch import (
    adapter_supports,
    declared_adapter_hooks,
    dispatch_lifecycle_action,
    dispatch_post_session,
    dispatch_pre_session,
)
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import SessionOutcome, SessionSpec, SubprocessEnvContribution
from agent_app.pack.contexts import LifecycleCtx
from agent_app.pack.manifest import DesiredPack
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.worker_supervisor import WorkerHandle

logger = logging.getLogger(__name__)


def _requests_host_resolution(connection_behavior: dict[str, Any] | None) -> bool:
    """Return True if the platform's connection_behavior asks the adapter to
    resolve a live connection target before starting Appium (e.g. an Android
    emulator registered as ``avd:<name>`` whose running ADB serial must be
    resolved at start time)."""
    if not connection_behavior:
        return False
    return connection_behavior.get("host_resolution_action") == "resolve"


READINESS_TIMEOUT = 30
READINESS_POLL_INTERVAL = 1
STOP_GRACE_PERIOD = 5
STOP_SESSION_DELETE_TIMEOUT = 5
AUTO_RESTART_DELAYS_SEC = (1, 2, 4, 8, 16, 30)
AUTO_RESTART_MAX_ATTEMPTS = 5
AUTO_RESTART_WINDOW_SEC = 300
MAX_RESTART_EVENTS = 200
APPIUM_DRIVER_CAPABILITY_DROP_KEYS = {
    "appium:connection_type",
    "appium:device_type",
    "appium:deviceName",
    "appium:manufacturer",
    "appium:model",
    "appium:os_version",
    "appium:platform",
    "connection_type",
    "device_type",
    "deviceName",
    "manufacturer",
    "model",
    "os_version",
    "platform",
}


def sanitize_appium_driver_capabilities(capabilities: dict[str, Any]) -> dict[str, Any]:
    """Drop GridFleet routing metadata before capabilities reach an Appium driver."""
    sanitized: dict[str, Any] = {}
    for key, value in capabilities.items():
        if key.startswith("gridfleet:"):
            continue
        if key in APPIUM_DRIVER_CAPABILITY_DROP_KEYS:
            continue
        sanitized[key] = value
    return sanitized


@dataclass
class AppiumInvocation:
    binary: str
    env_extra: dict[str, str] = field(default_factory=dict)


def _validate_appium_port_in_range(port: int) -> None:
    start = agent_settings.runtime.appium_port_range_start
    end = agent_settings.runtime.appium_port_range_end
    if port < start or port > end:
        raise InvalidStartPayloadError(f"Port {port} is outside configured Appium port range {start}-{end}")


def _loopback_appium_origin(port: int) -> httpx.URL:
    return httpx.URL(scheme="http", host="127.0.0.1", port=port)


def resolve_appium_invocation_for_pack(
    pack_id: str,
    registry: RuntimeRegistry | None,
) -> AppiumInvocation:
    """Return an AppiumInvocation for the given pack_id using the runtime registry.

    Raises RuntimeNotInstalledError if no installed runtime is found for *pack_id*.
    A host-global appium binary is never used as a fallback; run pack reconcile first.
    """
    if pack_id and registry is not None:
        env = registry.get_for_pack(pack_id)
        if env is not None:
            return AppiumInvocation(
                binary=env.appium_bin,
                env_extra={"APPIUM_HOME": env.appium_home},
            )
    raise RuntimeNotInstalledError(
        f"No runtime installed for pack {pack_id!r}; install the runtime via pack reconcile before starting Appium"
    )


def _macos_java_home() -> str | None:
    """Resolve a JDK home via /usr/libexec/java_home; None when unavailable."""
    try:
        result = subprocess.run(
            ["/usr/libexec/java_home"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        logger.debug("/usr/libexec/java_home probe failed", exc_info=True)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _find_java() -> str:
    """Find the java binary, checking PATH, JAVA_HOME, sdkman, and common locations."""
    found = shutil.which("java")
    if found and not (platform.system() == "Darwin" and os.path.realpath(found) == "/usr/bin/java"):
        return found
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = os.path.join(java_home, "bin", "java")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    if platform.system() == "Darwin" and found and os.path.realpath(found) == "/usr/bin/java":
        jh = _macos_java_home()
        candidate = os.path.join(jh, "bin", "java") if jh else ""
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    search_paths = [
        os.path.expanduser("~/.sdkman/candidates/java/current/bin"),
        "/usr/local/bin",
    ]
    # sdkman versioned directories
    sdkman_base = os.path.expanduser("~/.sdkman/candidates/java")
    if os.path.isdir(sdkman_base):
        for entry in sorted(os.listdir(sdkman_base), reverse=True):
            if entry == "current":
                continue
            candidate_dir = os.path.join(sdkman_base, entry, "bin")
            if candidate_dir not in search_paths:
                search_paths.append(candidate_dir)
    # macOS java_home
    if platform.system() == "Darwin":
        jh = _macos_java_home()
        if jh:
            search_paths.append(os.path.join(jh, "bin"))
    for search_dir in search_paths:
        candidate = os.path.join(search_dir, "java")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "java"


def build_env(
    *,
    appium_bin: str | None = None,
    appium_home: str | None = None,
    appium_env: dict[str, str] | None = None,
    adapter_env: SubprocessEnvContribution | None = None,
) -> dict[str, str]:
    """Build a subprocess env with appium and java on PATH."""
    env = os.environ.copy()
    extra_paths: list[str] = []

    if appium_bin is not None:
        bin_dir = os.path.dirname(appium_bin)
        if bin_dir and bin_dir not in env.get("PATH", ""):
            extra_paths.append(bin_dir)

    if adapter_env is not None:
        for d in adapter_env.extra_path_dirs:
            if d and d not in env.get("PATH", ""):
                extra_paths.append(d)
        # setdefault: host env wins; appium_env overrides later via update()
        for k, v in adapter_env.env_vars.items():
            env.setdefault(k, v)

    java_bin = _find_java()
    java_dir = os.path.dirname(java_bin)
    if java_dir and java_dir not in env.get("PATH", ""):
        extra_paths.append(java_dir)
    java_realpath = os.path.realpath(java_bin)
    if os.path.isabs(java_realpath) and os.path.isfile(java_realpath) and os.access(java_realpath, os.X_OK):
        java_home = os.path.dirname(os.path.dirname(java_realpath))
        if java_home:
            env.setdefault("JAVA_HOME", java_home)

    if extra_paths:
        env["PATH"] = os.pathsep.join(extra_paths) + os.pathsep + env.get("PATH", "")

    if appium_home is not None:
        env["APPIUM_HOME"] = appium_home

    if appium_env:
        env.update(appium_env)

    return env


@dataclass
class AppiumProcessInfo:
    port: int
    pid: int
    connection_target: str
    platform_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class AppiumLaunchSpec:
    connection_target: str
    port: int
    extra_caps: dict[str, Any] | None
    session_override: bool
    device_type: str | None
    ip_address: str | None
    pack_id: str
    platform_id: str
    pack_release: str | None = None
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: uuid.UUID | None = None
    appium_platform_name: str | None = None
    appium_env: dict[str, str] | None = None
    insecure_features: list[str] = field(default_factory=list)
    lifecycle_actions: list[dict[str, Any]] = field(default_factory=list)
    connection_behavior: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppiumStartFailure:
    port: int
    connection_target: str
    kind: str
    detail: str
    at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "connection_target": self.connection_target,
            "kind": self.kind,
            "detail": self.detail,
            "at": self.at,
        }


@dataclass(frozen=True)
class AppiumRestartEvent:
    sequence: int
    process: str
    kind: str
    port: int
    pid: int | None
    attempt: int
    delay_sec: int | None
    exit_code: int | None
    occurred_at: str
    will_retry: bool

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sequence": self.sequence,
            "process": self.process,
            "kind": self.kind,
            "port": self.port,
            "attempt": self.attempt,
            "occurred_at": self.occurred_at,
            "will_retry": self.will_retry,
        }
        if self.pid is not None:
            payload["pid"] = self.pid
        if self.delay_sec is not None:
            payload["delay_sec"] = self.delay_sec
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        return payload


class AppiumProcessManager:
    """Manages Appium server processes on this host."""

    def __init__(self) -> None:
        self._appium_procs: dict[int, asyncio.subprocess.Process] = {}  # appium_port -> process
        self._info: dict[int, AppiumProcessInfo] = {}
        self._launch_specs: dict[int, AppiumLaunchSpec] = {}
        self._log_maintenance_task: asyncio.Task[None] | None = None
        self._appium_watch_tasks: dict[int, asyncio.Task[None]] = {}
        self._appium_restart_tasks: dict[int, asyncio.Task[None]] = {}
        self._appium_restart_attempts: dict[int, collections.deque[float]] = {}
        self._appium_restart_backoff_steps: dict[int, int] = {}
        self._stop_pending_ports: set[int] = set()
        self._recent_restart_events: collections.deque[AppiumRestartEvent] = collections.deque(
            maxlen=MAX_RESTART_EVENTS
        )
        self._restart_sequence = 0
        self._start_failures: collections.deque[AppiumStartFailure] = collections.deque(maxlen=MAX_RESTART_EVENTS)
        self._intentional_stop_ports: set[int] = set()
        self._runtime_registry: RuntimeRegistry | None = None
        self._adapter_registry: AdapterRegistry | None = None
        self._desired_packs_provider: Callable[[], list[DesiredPack] | None] | None = None
        self._start_lock = asyncio.Lock()

    def set_runtime_registry(self, registry: RuntimeRegistry) -> None:
        self._runtime_registry = registry

    def set_adapter_registry(self, registry: AdapterRegistry) -> None:
        self._adapter_registry = registry

    def set_desired_packs_provider(self, provider: Callable[[], list[DesiredPack] | None]) -> None:
        self._desired_packs_provider = provider

    def retained_pack_worker_keys(self) -> set[tuple[str, str]]:
        """(pack_id, release) pairs pinned by current launch specs — the pack
        state loop must not reap these workers while their nodes exist."""
        return {
            (spec.pack_id, spec.pack_release) for spec in self._launch_specs.values() if spec.pack_release is not None
        }

    def _resolve_pack_worker(
        self, registry: AdapterRegistry, pack_id: str, *, requested_release: str | None
    ) -> tuple[str | None, WorkerHandle | None]:
        """Resolve the (release, worker) snapshot this start binds to — the
        worker serves both the resolve host-resolution dispatch and pre_session.

        The worker is None when the pack legitimately has no adapter — no
        tarball, or a tarball the loader inspected and marked adapterless
        (Tier-1 manifest-only pack); the release still identifies what was
        resolved so the locked revalidation can detect a swap. Raises
        StartDeferredError while that is not yet known or the desired release's
        worker is not loaded: pre_session is the only writer of the device
        connection caps (e.g. appium:udid), and a node started without them
        serves every udid-less session create from the driver's fallback —
        XCUITest creates and boots a Simulator on a real-device host. The
        desired release is required exactly: during an upgrade the old release's
        worker stays current while the new runtime is already registered, and a
        start in that window would bake stale adapter caps onto the new runtime
        with nothing restarting the mixed node later.
        """
        packs = self._desired_packs_provider() if self._desired_packs_provider is not None else None
        if packs is None:
            # Desired packs unknown (unwired provider, or before the pack state
            # loop's first fetch): fall back to the current handle; with nothing
            # loaded, defer rather than start a bare node. A launch pinned to a
            # release must not be served by a different release's handle.
            handle = registry.get_current(pack_id)
            if handle is None:
                raise StartDeferredError(f"driver-pack adapter for {pack_id!r} is not loaded yet")
            if requested_release is not None and handle.release != requested_release:
                raise StartDeferredError(
                    f"driver-pack adapter for {pack_id!r} release {requested_release!r} is not loaded yet"
                )
            return handle.release, handle
        desired = next((pack for pack in packs if pack.id == pack_id), None)
        if desired is None:
            # The list is known and omits the pack: it was disabled or retired.
            # The still-current worker is stale — do not let it serve a start
            # that races the pack cleanup.
            raise StartDeferredError(f"pack {pack_id!r} is not in the desired driver-pack list")
        if requested_release is not None and desired.release != requested_release:
            # The node poll and pack poll are independent; after a backend
            # release switch one cache refreshes before the other. Launch data
            # belongs to requested_release — pairing it with another release's
            # runtime/worker would persist a mixed node. Defer until both agree.
            raise StartDeferredError(
                f"pack {pack_id!r} release mismatch: launch expects {requested_release!r}, "
                f"desired list has {desired.release!r}"
            )
        if self._runtime_registry is not None and self._runtime_registry.release_for_pack(pack_id) != desired.release:
            # The desired list publishes before the runtime reconciles, and a
            # failed reconcile retains the previous env. Without this check an
            # artifact-less release bump would launch the old runtime with the
            # new release's payload (adapter packs are covered indirectly — the
            # new worker only loads after a successful runtime install).
            raise StartDeferredError(f"runtime for pack {pack_id!r} release {desired.release!r} is not installed yet")
        if not (desired.tarball_sha256 and desired.has_adapter_platform) or registry.is_adapterless(
            pack_id, desired.release
        ):
            # No adapter can ever exist for this release. If the manifest still
            # declares adapter-owned hooks that require an adapter it does not
            # ship, the start must not proceed — the dispatch would silently
            # no-op and Appium would spawn for a device whose hooks never ran.
            # The pack reports blocked for the same reason; the backend
            # desired-node endpoint keeps sending launch data regardless, so
            # the gate is here.
            required = declared_adapter_hooks(desired)
            if required:
                raise StartDeferredError(
                    f"pack {pack_id!r} declares hooks that require an adapter it does not ship: " + ", ".join(required)
                )
            return desired.release, None
        handle = registry.get(pack_id, desired.release)
        if handle is None:
            raise StartDeferredError(
                f"driver-pack adapter for {pack_id!r} release {desired.release!r} is not loaded yet"
            )
        return desired.release, handle

    def start_log_maintenance(self) -> None:
        """Sweep orphaned Appium log files and start the periodic size-cap pass."""
        sweep_log_dir()
        if self._log_maintenance_task is not None and not self._log_maintenance_task.done():
            return
        self._log_maintenance_task = asyncio.create_task(self._log_maintenance_loop())

    async def _log_maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(LOG_MAINTENANCE_INTERVAL_SEC)
            try:
                truncate_oversized_logs()
            except Exception as exc:  # maintenance must never kill the loop
                logger.warning("appium log maintenance pass failed: %s", sanitize_log_value(exc))

    def _cancel_task(self, tasks: dict[int, asyncio.Task[None]], port: int) -> None:
        task = tasks.pop(port, None)
        if task is None:
            return

        current = asyncio.current_task()
        if current is not None and task is current:
            tasks[port] = task
            return

        task.cancel()

    def _register_port_task(
        self,
        tasks: dict[int, asyncio.Task[None]],
        port: int,
        task: asyncio.Task[None],
    ) -> None:
        tasks[port] = task

        def _clear_finished(finished_task: asyncio.Task[None], *, task_port: int = port) -> None:
            if tasks.get(task_port) is finished_task:
                tasks.pop(task_port, None)

        task.add_done_callback(_clear_finished)

    def _running_info_for_target(
        self,
        *,
        connection_target: str,
        platform_id: str,
        exclude_port: int | None = None,
    ) -> AppiumProcessInfo | None:
        for info in self.list_running():
            if info.port == exclude_port:
                continue
            if info.connection_target == connection_target and info.platform_id == platform_id:
                return info
        return None

    def _trim_restart_attempts(
        self,
        restart_attempts: dict[int, collections.deque[float]],
        port: int,
        *,
        now: float | None = None,
    ) -> collections.deque[float]:
        history = restart_attempts.setdefault(port, collections.deque())
        current_time = now if now is not None else asyncio.get_running_loop().time()
        while history and current_time - history[0] > AUTO_RESTART_WINDOW_SEC:
            history.popleft()
        return history

    def _record_restart_event(
        self,
        *,
        process: str,
        kind: str,
        port: int,
        pid: int | None,
        attempt: int,
        delay_sec: int | None,
        exit_code: int | None,
        will_retry: bool,
    ) -> None:
        self._restart_sequence += 1
        self._recent_restart_events.append(
            AppiumRestartEvent(
                sequence=self._restart_sequence,
                process=process,
                kind=kind,
                port=port,
                pid=pid,
                attempt=attempt,
                delay_sec=delay_sec,
                exit_code=exit_code,
                occurred_at=datetime.now(UTC).isoformat(),
                will_retry=will_retry,
            )
        )

    def record_start_failure(self, *, port: int, connection_target: str, kind: str, detail: str) -> None:
        """Record a failed `start()` attempt for the next `/agent/health` payload.

        Level-style fact, not an event stream: the backend dedupes by (port, at),
        so unlike the restart-event ring this carries no sequence cursor.
        """
        self._start_failures.append(
            AppiumStartFailure(
                port=port,
                connection_target=connection_target,
                kind=kind,
                detail=detail,
                at=datetime.now(UTC).isoformat(),
            )
        )

    def _next_restart_delay(self, backoff_steps: dict[int, int], port: int) -> int:
        step = backoff_steps.get(port, 0)
        index = min(step, len(AUTO_RESTART_DELAYS_SEC) - 1)
        return AUTO_RESTART_DELAYS_SEC[index]

    def _advance_restart_backoff(self, backoff_steps: dict[int, int], port: int) -> None:
        current = backoff_steps.get(port, 0)
        backoff_steps[port] = min(current + 1, len(AUTO_RESTART_DELAYS_SEC) - 1)

    async def _watch_appium_process(self, port: int, process: asyncio.subprocess.Process) -> None:
        try:
            exit_code = await process.wait()
        except asyncio.CancelledError:
            raise

        if self._appium_procs.get(port) is not process:
            return
        if port in self._intentional_stop_ports:
            return

        info = self._info.get(port)
        connection_target = info.connection_target if info is not None else "unknown"
        logger.warning(
            "Appium process exited unexpectedly for connection_target=%s port=%d exit_code=%s",
            connection_target,
            port,
            exit_code,
        )
        restart_task = self._appium_restart_tasks.get(port)
        if restart_task is not None and not restart_task.done():
            return
        task = asyncio.create_task(self._auto_restart_appium(port, exit_code))
        self._register_port_task(self._appium_restart_tasks, port, task)

    async def _auto_restart_appium(self, port: int, exit_code: int | None) -> None:
        last_exit_code = exit_code
        while True:
            if port in self._intentional_stop_ports or port in self._stop_pending_ports:
                return

            history = self._trim_restart_attempts(self._appium_restart_attempts, port)
            next_attempt = len(history) + 1
            can_retry = next_attempt <= AUTO_RESTART_MAX_ATTEMPTS
            delay_sec = self._next_restart_delay(self._appium_restart_backoff_steps, port) if can_retry else None
            info = self._info.get(port)
            self._record_restart_event(
                process="appium",
                kind="crash_detected",
                port=port,
                pid=info.pid if info is not None else None,
                attempt=next_attempt,
                delay_sec=delay_sec,
                exit_code=last_exit_code,
                will_retry=can_retry,
            )
            if not can_retry:
                logger.error(
                    "Appium auto-restart exhausted for connection_target=%s port=%d after %d attempts in %ds",
                    info.connection_target if info is not None else "unknown",
                    port,
                    AUTO_RESTART_MAX_ATTEMPTS,
                    AUTO_RESTART_WINDOW_SEC,
                )
                self._record_restart_event(
                    process="appium",
                    kind="restart_exhausted",
                    port=port,
                    pid=info.pid if info is not None else None,
                    attempt=next_attempt,
                    delay_sec=None,
                    exit_code=last_exit_code,
                    will_retry=False,
                )
                return

            logger.info(
                "Scheduling Appium auto-restart for connection_target=%s port=%d attempt=%d delay_sec=%d",
                info.connection_target if info is not None else "unknown",
                port,
                next_attempt,
                delay_sec,
            )
            assert delay_sec is not None
            await asyncio.sleep(delay_sec)
            if port in self._intentional_stop_ports:
                return
            if port in self._stop_pending_ports:
                # Operator queued a stop_pending lifecycle during the
                # backoff window; honor it instead of resurrecting the
                # process. The actual stop is owned by the backend's appium
                # reconciler via AppiumNode.desired_state.
                return
            if port not in self._launch_specs:
                return

            history = self._trim_restart_attempts(self._appium_restart_attempts, port)
            attempt_number = len(history) + 1
            history.append(asyncio.get_running_loop().time())
            try:
                restarted = await self._restart_from_launch_spec(port)
            except PortOccupiedError:
                self._record_restart_event(
                    process="appium",
                    kind="port_conflict",
                    port=port,
                    pid=info.pid if info is not None else None,
                    attempt=attempt_number,
                    delay_sec=None,
                    exit_code=last_exit_code,
                    will_retry=False,
                )
                logger.error(
                    "Appium auto-restart stopped for connection_target=%s port=%d because the port is occupied",
                    info.connection_target if info is not None else "unknown",
                    port,
                )
                await self._drop_failed_managed_port(port)
                return
            except AlreadyRunningError:
                # Another node already serves this target on a different port
                # (e.g. the backend restarted it elsewhere during the crash
                # window). Resurrecting this port would only re-raise forever and
                # risk a duplicate node, so abort and release the port; the
                # backend reconciler reaps whichever node is the true orphan.
                logger.info(
                    "Appium auto-restart aborted for port %d: target %s already served by another node",
                    port,
                    info.connection_target if info is not None else "unknown",
                )
                await self._drop_failed_managed_port(port)
                return
            except StartDeferredError as exc:
                # Start could not proceed yet (adapter/runtime still loading, or
                # release changed mid-start). This is transient, not a restart
                # failure: do not advance the auto-restart backoff or drop the
                # port. The node-state convergence loop retries start() next tick
                # and defers again until the transient condition clears.
                logger.info(
                    "Appium auto-restart deferred for port %d: %s; convergence loop will retry",
                    port,
                    exc,
                )
                return
            except Exception:
                self._advance_restart_backoff(self._appium_restart_backoff_steps, port)
                logger.exception("Appium auto-restart failed for port %d on attempt %d", port, attempt_number)
                last_exit_code = None
                continue

            self._appium_restart_backoff_steps.pop(port, None)
            self._record_restart_event(
                process="appium",
                kind="restart_succeeded",
                port=port,
                pid=restarted.pid,
                attempt=attempt_number,
                delay_sec=delay_sec,
                exit_code=last_exit_code,
                will_retry=False,
            )
            logger.info(
                "Appium auto-restart succeeded for connection_target=%s port=%d attempt=%d pid=%d",
                restarted.connection_target,
                restarted.port,
                attempt_number,
                restarted.pid,
            )
            return

    async def _restart_from_launch_spec(self, port: int) -> AppiumProcessInfo:
        spec = self._launch_specs.get(port)
        if spec is None:
            raise RuntimeError(f"No launch spec found for port {port}")
        return await self.start(**asdict(spec))

    async def _start_appium_server(
        self,
        spec: AppiumLaunchSpec,
        *,
        clear_logs_on_failure: bool,
        pack_worker: WorkerHandle | None = None,
    ) -> asyncio.subprocess.Process:
        appium_platform = spec.appium_platform_name or spec.platform_id
        extra_caps = dict(spec.extra_caps) if spec.extra_caps is not None else None

        caps = {"platformName": appium_platform}
        if extra_caps:
            caps.update(extra_caps)
        caps = sanitize_appium_driver_capabilities(caps)

        invocation = resolve_appium_invocation_for_pack(pack_id=spec.pack_id, registry=self._runtime_registry)
        # The caller's resolved worker snapshot — not a fresh get_current()
        # lookup, which can still point at a retired release's worker during an
        # adapter→adapterless transition and leak its subprocess env.
        adapter_env = getattr(pack_worker, "subprocess_env", None) if pack_worker is not None else None
        if callable(adapter_env):
            adapter_env = adapter_env()
        env = build_env(
            appium_bin=invocation.binary,
            appium_home=invocation.env_extra.get("APPIUM_HOME"),
            appium_env=spec.appium_env,
            adapter_env=adapter_env,
        )
        appium_bin = invocation.binary

        appium_cmd = [
            appium_bin,
            "server",
            "--port",
            str(spec.port),
            "--default-capabilities",
            json.dumps(caps),
        ]
        if spec.session_override:
            appium_cmd.append("--session-override")
        if spec.insecure_features:
            appium_cmd.extend(["--allow-insecure", ",".join(spec.insecure_features)])

        if await self._can_connect_to_appium(spec.port):
            raise PortOccupiedError(
                f"Port {spec.port} is already in use by another Appium listener; "
                "stop the existing process before starting a new managed node"
            )

        if not self._is_appium_port_bindable(spec.port):
            # Appium binds on 0.0.0.0; a non-Appium listener on this port would
            # otherwise pass the HTTP probe above, then fail subprocess bind
            # with EADDRINUSE — surfacing as a 30s readiness timeout instead of
            # a fast 409.
            raise PortOccupiedError(
                f"Port {spec.port} is already bound on this host by a non-Appium listener; "
                "stop the existing process before starting a new managed node"
            )

        log_file = open_log_file(spec.port)
        try:
            appium_proc = await asyncio.create_subprocess_exec(
                *appium_cmd,
                stdout=log_file,
                stderr=log_file,
                env=env,
            )
        except FileNotFoundError:
            raise RuntimeMissingError(f"appium executable not found (last tried: {appium_bin})") from None
        finally:
            # Only the agent's copy of the fd — the child inherited its own.
            log_file.close()

        ready = await self._wait_for_readiness(spec.port, appium_proc)
        if not ready:
            try:
                appium_proc.kill()
                await appium_proc.wait()
            except ProcessLookupError:
                logger.debug("Appium process on port %d already exited before kill", spec.port, exc_info=True)
            recent_logs = tail_lines(appium_log_path(spec.port), 20)
            if clear_logs_on_failure:
                appium_log_path(spec.port).unlink(missing_ok=True)
            log_snippet = "\n".join(recent_logs) if recent_logs else "(no output captured)"
            raise StartupTimeoutError(
                f"Appium on port {spec.port} did not become ready within {READINESS_TIMEOUT}s. Output:\n{log_snippet}"
            )

        self._appium_procs[spec.port] = appium_proc
        self._cancel_task(self._appium_watch_tasks, spec.port)
        self._register_port_task(
            self._appium_watch_tasks,
            spec.port,
            asyncio.create_task(self._watch_appium_process(spec.port, appium_proc)),
        )
        return appium_proc

    async def start(
        self,
        connection_target: str,
        platform_id: str,
        port: int,
        *,
        pack_id: str,
        extra_caps: dict[str, Any] | None = None,
        pack_release: str | None = None,
        accepting_new_sessions: bool = True,
        stop_pending: bool = False,
        grid_run_id: uuid.UUID | None = None,
        session_override: bool = True,
        device_type: str | None = None,
        ip_address: str | None = None,
        appium_platform_name: str | None = None,
        appium_env: dict[str, str] | None = None,
        insecure_features: list[str] | None = None,
        lifecycle_actions: list[dict[str, Any]] | None = None,
        connection_behavior: dict[str, Any] | None = None,
    ) -> AppiumProcessInfo:
        if port in self._appium_procs and self._appium_procs[port].returncode is None:
            raise AlreadyRunningError(f"Appium already running on port {port}")
        if not pack_id or not platform_id:
            raise InvalidStartPayloadError("Appium start requires pack_id and platform_id")
        _validate_appium_port_in_range(port)
        self._cancel_task(self._appium_restart_tasks, port)
        resolved_connection_target = connection_target
        pack_worker: WorkerHandle | None = None
        pack_worker_release: str | None = None
        if self._adapter_registry is not None:
            pack_worker_release, pack_worker = self._resolve_pack_worker(
                self._adapter_registry, pack_id, requested_release=pack_release
            )
        if _requests_host_resolution(connection_behavior):
            if (
                self._adapter_registry is None
                or pack_worker is None
                or not adapter_supports(pack_worker, "lifecycle_action")
            ):
                raise StartDeferredError(f"driver-pack adapter for {pack_id!r} cannot perform host resolution")
            lifecycle_result = await dispatch_lifecycle_action(
                pack_worker,
                "resolve",
                {"device_type": device_type},
                LifecycleCtx(host_id="", device_identity_value=connection_target),
            )
            if lifecycle_result.resolved_connection_target:
                resolved_connection_target = lifecycle_result.resolved_connection_target
            elif not lifecycle_result.ok:
                raise DeviceNotFoundError(lifecycle_result.detail or f"{connection_target!r} could not be resolved")
            else:
                raise DeviceNotFoundError(f"resolve for {connection_target!r} did not return a serial")
        merged_extra_caps = dict(extra_caps) if extra_caps else {}
        if pack_worker is not None:
            if not adapter_supports(pack_worker, "pre_session"):
                adapter_caps = {}
            else:
                adapter_caps = await dispatch_pre_session(
                    pack_worker,
                    SessionSpec(
                        pack_id=pack_id,
                        platform_id=platform_id,
                        device_identity_value=resolved_connection_target,
                        capabilities=merged_extra_caps,
                    ),
                )
            merged_extra_caps.update(adapter_caps)
        spec = AppiumLaunchSpec(
            connection_target=resolved_connection_target,
            port=port,
            extra_caps=merged_extra_caps if merged_extra_caps else None,
            accepting_new_sessions=accepting_new_sessions,
            stop_pending=stop_pending,
            grid_run_id=grid_run_id,
            session_override=session_override,
            device_type=device_type,
            ip_address=ip_address,
            pack_id=pack_id,
            platform_id=platform_id,
            # Pin the release this start resolved (covers legacy unversioned
            # payloads too) so an auto-restart replays against the same release
            # and defers on a mismatch instead of rebinding old launch data.
            pack_release=pack_worker_release if self._adapter_registry is not None else pack_release,
            appium_platform_name=appium_platform_name,
            appium_env=dict(appium_env) if appium_env else None,
            insecure_features=list(insecure_features) if insecure_features else [],
            lifecycle_actions=list(lifecycle_actions) if lifecycle_actions else [],
            connection_behavior=dict(connection_behavior) if connection_behavior else {},
        )
        async with self._start_lock:
            if port in self._appium_procs and self._appium_procs[port].returncode is None:
                raise AlreadyRunningError(f"Appium already running on port {port}")
            duplicate = self._running_info_for_target(
                connection_target=resolved_connection_target,
                platform_id=platform_id,
                exclude_port=port,
            )
            if duplicate is not None:
                raise AlreadyRunningError(
                    f"Appium already running for target {resolved_connection_target!r} on port {duplicate.port}"
                )

            if self._adapter_registry is not None:
                # Revalidate under the lock: a pack-loop reconcile may have
                # published a different release while this start awaited
                # resolve, pre_session, or the lock. The resolved target and
                # merged caps belong to the snapshot resolved above;
                # spawning them onto a swapped runtime would persist a mixed
                # node. Defer so the retry re-derives everything against the
                # new release.
                revalidated_release, revalidated_worker = self._resolve_pack_worker(
                    self._adapter_registry, pack_id, requested_release=pack_release
                )
                if revalidated_release != pack_worker_release or (revalidated_worker is None) != (pack_worker is None):
                    raise StartDeferredError(f"driver-pack release for {pack_id!r} changed during start")

            self._launch_specs[port] = spec
            self._intentional_stop_ports.discard(port)
            # Honor the operator's stop_pending intent across restarts: when
            # the caller asks for a stop-pending lifecycle (e.g. auto-restart
            # carrying spec.stop_pending forward, or a fresh start that should
            # stop after the next session), keep the port in
            # `_stop_pending_ports`. Otherwise clear any stale intent left by a
            # prior lifecycle.
            if spec.stop_pending:
                self._stop_pending_ports.add(port)
            else:
                self._stop_pending_ports.discard(port)
            appium_proc = await self._start_appium_server(
                spec, clear_logs_on_failure=port not in self._info, pack_worker=pack_worker
            )
            started_at = datetime.now(UTC)

            info = self._info.get(port)
            if info is None:
                info = AppiumProcessInfo(
                    port=port,
                    pid=appium_proc.pid,
                    connection_target=resolved_connection_target,
                    platform_id=platform_id,
                    started_at=started_at,
                )
                self._info[port] = info
            else:
                info.pid = appium_proc.pid
                info.connection_target = resolved_connection_target
                info.platform_id = platform_id
                info.started_at = started_at
            if spec.stop_pending:
                # Carry the stop-pending flag so ``_auto_restart_appium``
                # refuses to resurrect this Appium process if it exits. The
                # actual stop is owned by the backend's appium reconciler
                # via ``AppiumNode.desired_state``.
                self._stop_pending_ports.add(port)
            return info

    async def reconfigure(
        self,
        port: int,
        *,
        accepting_new_sessions: bool,
        stop_pending: bool,
        grid_run_id: uuid.UUID | None,
    ) -> None:
        if port not in self._info:
            raise DeviceNotFoundError(f"No managed Appium process is running on port {port}")

        spec = self._launch_specs.get(port)
        if spec is not None:
            self._launch_specs[port] = replace(
                spec,
                accepting_new_sessions=accepting_new_sessions,
                stop_pending=stop_pending,
                grid_run_id=grid_run_id,
            )

        if stop_pending:
            # Track the port so ``_auto_restart_appium`` skips resurrection if
            # the Appium process exits while a stop is pending. The actual
            # stop is owned by the backend's appium reconciler, which
            # converges based on ``AppiumNode.desired_state``.
            self._stop_pending_ports.add(port)
        else:
            self._stop_pending_ports.discard(port)

    def _forget_port(self, port: int) -> asyncio.subprocess.Process | None:
        """Cancel per-port tasks and drop all bookkeeping; returns the popped process."""
        self._cancel_task(self._appium_restart_tasks, port)
        self._cancel_task(self._appium_watch_tasks, port)
        proc = self._appium_procs.pop(port, None)
        self._info.pop(port, None)
        self._launch_specs.pop(port, None)
        self._appium_restart_attempts.pop(port, None)
        self._appium_restart_backoff_steps.pop(port, None)
        return proc

    async def _drop_failed_managed_port(self, port: int) -> None:
        """Forget stale ownership for a crashed Appium process.

        Release the Appium process and port metadata so the host can recover
        the port.
        """
        self._forget_port(port)

    async def _dispatch_post_session(self, spec: AppiumLaunchSpec | None) -> None:
        """Fire the adapter ``post_session`` cleanup hook for a stopped node.

        Symmetric counterpart to the ``adapter_pre_session`` call in the start
        path. Runs outside the start lock; adapter failures must never wedge
        teardown (the node is already gone), so they are logged and swallowed.
        """
        if self._adapter_registry is None or spec is None or not spec.pack_id:
            return
        # Teardown belongs to the release the node was started from: after a
        # release switch get_current() points at the new adapter. Legacy specs
        # without a pinned release keep the current-worker fallback.
        if spec.pack_release is not None:
            handle = self._adapter_registry.get(spec.pack_id, spec.pack_release)
        else:
            handle = self._adapter_registry.get_current(spec.pack_id)
        if handle is None:
            return
        if not adapter_supports(handle, "post_session"):
            return
        try:
            await dispatch_post_session(
                handle,
                SessionSpec(
                    pack_id=spec.pack_id,
                    platform_id=spec.platform_id,
                    device_identity_value=spec.connection_target,
                ),
                SessionOutcome(ok=True, detail="stopped"),
            )
        except Exception as exc:
            logger.warning("adapter post_session failed for pack %s: %s", spec.pack_id, sanitize_log_value(exc))

    async def _delete_active_sessions(self, port: int) -> None:
        """Best-effort DELETE of live W3C sessions before terminating Appium.

        Deleting the session while the server is still responsive lets the
        driver release its own per-session resources — notably the adb
        port-forwards (systemPort/mjpegServerPort/chromedriverPort) — instead of
        orphaning them when the process is SIGKILLed or misses the SIGTERM grace
        window. Driver-agnostic: a plain W3C ``DELETE /session/{id}``. Errors are
        swallowed; teardown must proceed regardless.
        """
        client = http_client.get_client()
        origin = _loopback_appium_origin(port)
        try:
            resp = await client.get(origin.join("/appium/sessions"), timeout=2)
        except httpx.HTTPError:
            return
        if resp.status_code != 200:
            return
        payload: object = resp.json()
        if not isinstance(payload, dict):
            return
        value = payload.get("value")
        if not isinstance(value, list):
            return
        for session in value:
            if not isinstance(session, dict):
                continue
            session_id = session.get("id")
            if not isinstance(session_id, str):
                continue
            with contextlib.suppress(httpx.HTTPError):
                await client.delete(origin.join(f"/session/{session_id}"), timeout=STOP_SESSION_DELETE_TIMEOUT)

    async def stop(self, port: int) -> None:
        async with self._start_lock:
            self._intentional_stop_ports.add(port)
            spec = self._launch_specs.get(port)
            appium_proc = self._forget_port(port)

            if appium_proc and appium_proc.returncode is None:
                await self._delete_active_sessions(port)
                appium_proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(appium_proc.wait(), timeout=STOP_GRACE_PERIOD)
                except TimeoutError:
                    appium_proc.kill()
                    await appium_proc.wait()

            appium_log_path(port).unlink(missing_ok=True)
            self._intentional_stop_ports.discard(port)
        await self._dispatch_post_session(spec)

    async def status(self, port: int) -> dict[str, Any]:
        proc = self._appium_procs.get(port)
        if proc is None or proc.returncode is not None:
            return {"running": False, "port": port}

        appium_status = await self._fetch_appium_status(port)
        if appium_status is None:
            return {"running": False, "port": port}
        return {"running": True, "port": port, "pid": proc.pid, "appium_status": appium_status}

    def get_logs(self, port: int, lines: int = 100) -> list[str]:
        return tail_lines(appium_log_path(port), lines)

    def list_running(self) -> list[AppiumProcessInfo]:
        running: list[AppiumProcessInfo] = []
        for port, info in self._info.items():
            proc = self._appium_procs.get(port)
            if proc is not None and proc.returncode is None:
                running.append(info)
        return running

    async def process_snapshot(self) -> dict[str, Any]:
        return {
            "running_nodes": [await self._running_node_snapshot(info) for info in self.list_running()],
            "recent_restart_events": [event.to_payload() for event in self._recent_restart_events],
            "start_failures": [failure.to_payload() for failure in self._start_failures],
        }

    async def _running_node_snapshot(self, info: AppiumProcessInfo) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "port": info.port,
            "pid": info.pid,
            "connection_target": info.connection_target,
            "platform_id": info.platform_id,
            "started_at": info.started_at.isoformat(),
        }
        spec = self._launch_specs.get(info.port)
        if spec is not None and spec.pack_release is not None:
            # The release this node was started from drives backend pack rollouts.
            payload["pack_release"] = spec.pack_release
        # Re-emit has_active_session for the agent self-update drain gate (harness C1).
        # The grid relay that used to track sessions is gone, so the only authoritative
        # source is Appium itself: query localhost GET /appium/sessions per running node.
        # An enumeration failure (Appium down, or the node lacks session_discovery) omits
        # the key, which update.py counts as "unknown" => a drain blocker — the safe
        # default (never kill an in-flight session on uncertainty). The backend now
        # force-injects session_discovery into every started node (harness C10), so the
        # enumeration is reliable for grid packs and the omit case no longer wedges.
        active = await self._node_has_active_session(info.port)
        if active is not None:
            payload["has_active_session"] = active
        return payload

    async def _node_has_active_session(self, port: int) -> bool | None:
        """Whether the localhost Appium on *port* reports any live W3C session.

        ``None`` means the count is unknown (Appium unreachable, non-200, or the node
        lacks the ``session_discovery`` insecure feature) — callers must treat unknown
        as a blocker, not as "no sessions".
        """
        client = http_client.get_client()
        url = _loopback_appium_origin(port).join("/appium/sessions")
        try:
            resp = await client.get(url, timeout=2)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        payload: object = resp.json()
        if not isinstance(payload, dict):
            return None
        value = payload.get("value")
        if not isinstance(value, list):
            return None
        return len(value) > 0

    async def shutdown(self) -> None:
        ports = sorted(set(self._appium_procs) | set(self._launch_specs))
        for port in ports:
            with contextlib.suppress(Exception):
                await self.stop(port)
        for task_map in (self._appium_restart_tasks, self._appium_watch_tasks):
            for task in task_map.values():
                task.cancel()
            task_map.clear()
        if self._log_maintenance_task is not None:
            self._log_maintenance_task.cancel()
            self._log_maintenance_task = None

    async def _fetch_appium_status(self, port: int) -> dict[str, Any] | None:
        client = http_client.get_client()
        url = _loopback_appium_origin(port).join("/status")
        try:
            resp = await client.get(url, timeout=2)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        payload: object = resp.json()
        if not isinstance(payload, dict):
            return None
        return {str(key): value for key, value in payload.items()}

    async def _can_connect_to_appium(self, port: int) -> bool:
        return await self._fetch_appium_status(port) is not None

    def _is_appium_port_bindable(self, port: int) -> bool:
        # Mirror Appium's own bind (0.0.0.0, SO_REUSEADDR) so the probe returns
        # the same EADDRINUSE outcome the subprocess would hit. Appium's runtime
        # (Node/libuv) sets SO_REUSEADDR on its listener; omitting it here makes
        # the probe stricter than reality — a restart that rebinds a just-vacated
        # port false-positives on the previous process's TIME_WAIT connection
        # sockets and the agent gives up on the same-port restart (N12). A live
        # listener still fails bind (SO_REUSEADDR does not override an active
        # LISTEN), so genuine port squatters are still detected. The probe never
        # calls listen()/accept() — the socket is closed by the context manager
        # immediately after bind, so no traffic from any interface can reach it.
        # (CodeQL py/bind-socket-all-network-interfaces flags the literal
        # 0.0.0.0; the alert is dismissed as a false positive — see PR #283.)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                return False
        return True

    async def _wait_for_readiness(self, port: int, process: asyncio.subprocess.Process) -> bool:
        deadline = asyncio.get_event_loop().time() + READINESS_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if process.returncode is not None:
                return False
            if await self._can_connect_to_appium(port):
                return True
            await asyncio.sleep(READINESS_POLL_INTERVAL)
        return False
