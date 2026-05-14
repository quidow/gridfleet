import asyncio
import collections
import contextlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from agent_app.config import agent_settings
from agent_app.grid_node.config import GridNodeConfig
from agent_app.grid_node.event_bus import EventBus
from agent_app.grid_node.protocol import build_slots
from agent_app.grid_node.service import GridNodeService
from agent_app.grid_node.supervisor import GridNodeSupervisorHandle, start_grid_node_supervisor
from agent_app.grid_url import get_local_ip
from agent_app.observability import sanitize_log_value
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.dispatch import adapter_lifecycle_action, adapter_pre_session
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.tools.utils import _find_adb, find_android_home

logger = logging.getLogger(__name__)


def _has_lifecycle_action(actions: list[dict[str, Any]], action_id: str) -> bool:
    """Return True if any action in *actions* has id == *action_id*."""
    return any(action.get("id") == action_id for action in actions)


READINESS_TIMEOUT = 30
READINESS_POLL_INTERVAL = 1
STOP_GRACE_PERIOD = 5
MAX_LOG_LINES = 5000
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
        if key.startswith("gridfleet:") or key.startswith("appium:gridfleet:"):
            continue
        if key in APPIUM_DRIVER_CAPABILITY_DROP_KEYS:
            continue
        sanitized[key] = value
    return sanitized


@dataclass
class AppiumInvocation:
    binary: str
    env_extra: dict[str, str] = field(default_factory=dict)


class RuntimeNotInstalledError(RuntimeError):
    """Raised when no runtime is installed for the requested pack."""


class PortOccupiedError(RuntimeError):
    """Raised when a managed Appium port is now owned by another listener."""


class AlreadyRunningError(RuntimeError):
    """A managed Appium is already running on this port."""


class StartupTimeoutError(RuntimeError):
    """Appium failed to become ready before the timeout."""


class RuntimeMissingError(RuntimeError):
    """Required runtime tools are not installed on the host."""


class InvalidStartPayloadError(RuntimeError):
    """Start payload is missing required fields."""


class DeviceNotFoundError(RuntimeError):
    """Connection target is not visible to the host adapter."""


def _validate_appium_port_in_range(port: int) -> None:
    start = agent_settings.appium_port_range_start
    end = agent_settings.appium_port_range_end
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
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                jh = result.stdout.strip()
                candidate = os.path.join(jh, "bin", "java") if jh else ""
                if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("/usr/libexec/java_home probe failed", exc_info=True)
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
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                jh = result.stdout.strip()
                if jh:
                    search_paths.append(os.path.join(jh, "bin"))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("/usr/libexec/java_home search probe failed", exc_info=True)
    for search_dir in search_paths:
        candidate = os.path.join(search_dir, "java")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "java"


def _build_env(
    platform_name: str | None = None,
    device_type: str | None = None,
    *,
    appium_bin: str | None = None,
    appium_home: str | None = None,
    workaround_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a subprocess env with appium, adb, and java on PATH."""
    env = os.environ.copy()
    extra_paths: list[str] = []

    if appium_bin is not None:
        bin_dir = os.path.dirname(appium_bin)
        if bin_dir and bin_dir not in env.get("PATH", ""):
            extra_paths.append(bin_dir)

    adb_dir = os.path.dirname(_find_adb())
    if adb_dir and adb_dir not in env.get("PATH", ""):
        extra_paths.append(adb_dir)

    # Set ANDROID_HOME / ANDROID_SDK_ROOT so Appium drivers can find the SDK
    android_home = find_android_home()
    if android_home:
        env.setdefault("ANDROID_HOME", android_home)
        env.setdefault("ANDROID_SDK_ROOT", android_home)

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

    if workaround_env:
        env.update(workaround_env)

    return env


@dataclass
class AppiumProcessInfo:
    port: int
    pid: int
    connection_target: str
    platform_id: str


@dataclass(frozen=True)
class AppiumLaunchSpec:
    connection_target: str
    port: int
    plugins: list[str] | None
    extra_caps: dict[str, Any] | None
    stereotype_caps: dict[str, Any] | None
    session_override: bool
    device_type: str | None
    ip_address: str | None
    manage_grid_node: bool
    pack_id: str
    platform_id: str
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: uuid.UUID | None = None
    appium_platform_name: str | None = None
    workaround_env: dict[str, str] | None = None
    insecure_features: list[str] = field(default_factory=list)
    grid_slots: list[str] = field(default_factory=lambda: ["native"])
    lifecycle_actions: list[dict[str, Any]] = field(default_factory=list)
    connection_behavior: dict[str, Any] = field(default_factory=dict)
    headless: bool = True


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
    """Manages Appium server processes and in-process Grid node services on this host."""

    def __init__(self) -> None:
        self._appium_procs: dict[int, asyncio.subprocess.Process] = {}  # appium_port -> process
        self._grid_supervisors: dict[int, GridNodeSupervisorHandle] = {}
        self._info: dict[int, AppiumProcessInfo] = {}
        self._launch_specs: dict[int, AppiumLaunchSpec] = {}
        self._logs: dict[int, collections.deque[str]] = {}
        self._log_tasks: dict[int, list[asyncio.Task[None]]] = {}
        self._appium_watch_tasks: dict[int, asyncio.Task[None]] = {}
        self._appium_restart_tasks: dict[int, asyncio.Task[None]] = {}
        self._appium_restart_attempts: dict[int, collections.deque[float]] = {}
        self._appium_restart_backoff_steps: dict[int, int] = {}
        self._stop_pending_ports: set[int] = set()
        self._stop_pending_tasks: dict[int, asyncio.Task[None]] = {}
        self._recent_restart_events: collections.deque[AppiumRestartEvent] = collections.deque(
            maxlen=MAX_RESTART_EVENTS
        )
        self._restart_sequence = 0
        self._intentional_stop_ports: set[int] = set()
        self._next_node_port = agent_settings.grid_node_port_start
        self._runtime_registry: RuntimeRegistry | None = None
        self._adapter_registry: AdapterRegistry | None = None
        self._start_lock = asyncio.Lock()
        self._grid_advertise_ip: str | None = None

    def set_runtime_registry(self, registry: RuntimeRegistry) -> None:
        self._runtime_registry = registry

    def set_adapter_registry(self, registry: AdapterRegistry) -> None:
        self._adapter_registry = registry

    async def _read_stream(self, port: int, stream: asyncio.StreamReader, prefix: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\n")
            if port in self._logs:
                self._logs[port].append(f"[{prefix}] {text}")

    def _allocate_node_port(self) -> int:
        # Skip ports already bound by another listener so the grid node HTTP
        # server does not race a third-party process on a stale port. Probe
        # the same interface uvicorn will bind (`grid_node_bind_host`,
        # default `0.0.0.0`) — NOT the advertised hostname, which can be a
        # docker-only DNS name like `host.docker.internal` that does not
        # resolve on the agent host and would fail probe + bind alike.
        probe_host = getattr(agent_settings, "grid_node_bind_host", "0.0.0.0")
        while True:
            port = self._next_node_port
            self._next_node_port += 1
            with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
                try:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind((probe_host, port))
                except OSError:
                    continue
            return port

    def _track_stream_logs(
        self,
        port: int,
        process: asyncio.subprocess.Process,
        *,
        prefix: str,
    ) -> list[asyncio.Task[None]]:
        tasks: list[asyncio.Task[None]] = []
        if process.stdout:
            tasks.append(asyncio.create_task(self._read_stream(port, process.stdout, prefix)))
        if process.stderr:
            tasks.append(asyncio.create_task(self._read_stream(port, process.stderr, prefix)))
        self._log_tasks.setdefault(port, []).extend(tasks)
        return tasks

    def _remove_log_tasks(self, port: int, tasks: list[asyncio.Task[None]]) -> None:
        existing = self._log_tasks.get(port)
        if existing is None:
            return
        self._log_tasks[port] = [task for task in existing if task not in tasks]
        if not self._log_tasks[port]:
            self._log_tasks.pop(port, None)

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

    def _grid_external_url(self, node_port: int) -> str:
        if self._grid_advertise_ip is None:
            self._grid_advertise_ip = get_local_ip()
        return f"http://{self._grid_advertise_ip}:{node_port}"

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
            if port not in self._launch_specs:
                return

            attempt_number = (
                len(
                    self._trim_restart_attempts(
                        self._appium_restart_attempts,
                        port,
                        now=asyncio.get_running_loop().time(),
                    )
                )
                + 1
            )
            self._appium_restart_attempts.setdefault(port, collections.deque()).append(
                asyncio.get_running_loop().time()
            )
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
        return await self.start(
            connection_target=spec.connection_target,
            platform_id=spec.platform_id,
            port=spec.port,
            grid_url=agent_settings.grid_hub_url,
            plugins=spec.plugins,
            extra_caps=spec.extra_caps,
            stereotype_caps=spec.stereotype_caps,
            accepting_new_sessions=spec.accepting_new_sessions,
            stop_pending=spec.stop_pending,
            grid_run_id=spec.grid_run_id,
            session_override=spec.session_override,
            device_type=spec.device_type,
            ip_address=spec.ip_address,
            manage_grid_node=spec.manage_grid_node,
            pack_id=spec.pack_id,
            appium_platform_name=spec.appium_platform_name,
            workaround_env=spec.workaround_env,
            insecure_features=spec.insecure_features,
            grid_slots=spec.grid_slots,
            lifecycle_actions=list(spec.lifecycle_actions),
            connection_behavior=dict(spec.connection_behavior),
        )

    async def _start_appium_server(
        self,
        spec: AppiumLaunchSpec,
        *,
        clear_logs_on_failure: bool,
    ) -> asyncio.subprocess.Process:
        appium_platform = spec.appium_platform_name or spec.platform_id
        extra_caps = dict(spec.extra_caps) if spec.extra_caps is not None else None

        caps = {"appium:udid": spec.connection_target, "platformName": appium_platform}
        if extra_caps:
            caps.update(extra_caps)
        caps = sanitize_appium_driver_capabilities(caps)

        invocation = resolve_appium_invocation_for_pack(pack_id=spec.pack_id, registry=self._runtime_registry)
        env = _build_env(
            platform_name=spec.platform_id,
            device_type=spec.device_type,
            appium_bin=invocation.binary,
            appium_home=invocation.env_extra.get("APPIUM_HOME"),
            workaround_env=spec.workaround_env,
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
        if spec.plugins:
            appium_cmd.extend(["--use-plugins", ",".join(spec.plugins)])
        if spec.insecure_features:
            appium_cmd.extend(["--allow-insecure", ",".join(spec.insecure_features)])

        if await self._can_connect_to_appium(spec.port):
            raise PortOccupiedError(
                f"Port {spec.port} is already in use by another Appium listener; "
                "stop the existing process before starting a new managed node"
            )

        try:
            appium_proc = await asyncio.create_subprocess_exec(
                *appium_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            raise RuntimeMissingError(f"appium executable not found (last tried: {appium_bin})") from None

        self._logs.setdefault(spec.port, collections.deque(maxlen=MAX_LOG_LINES))
        log_tasks = self._track_stream_logs(spec.port, appium_proc, prefix="appium")

        ready = await self._wait_for_readiness(spec.port, appium_proc)
        if not ready:
            await asyncio.sleep(0.5)
            recent_logs = list(self._logs.get(spec.port, []))[-20:]
            try:
                appium_proc.kill()
                await appium_proc.wait()
            except ProcessLookupError:
                logger.debug("Appium process on port %d already exited before kill", spec.port, exc_info=True)
            for task in log_tasks:
                task.cancel()
            self._remove_log_tasks(spec.port, log_tasks)
            if clear_logs_on_failure:
                self._logs.pop(spec.port, None)
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
        grid_url: str,
        *,
        pack_id: str,
        plugins: list[str] | None = None,
        extra_caps: dict[str, Any] | None = None,
        stereotype_caps: dict[str, Any] | None = None,
        accepting_new_sessions: bool = True,
        stop_pending: bool = False,
        grid_run_id: uuid.UUID | None = None,
        session_override: bool = True,
        device_type: str | None = None,
        ip_address: str | None = None,
        manage_grid_node: bool = True,
        headless: bool = True,
        appium_platform_name: str | None = None,
        workaround_env: dict[str, str] | None = None,
        insecure_features: list[str] | None = None,
        grid_slots: list[str] | None = None,
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
        if (
            device_type in {"emulator", "simulator"}
            and self._adapter_registry is not None
            and _has_lifecycle_action(lifecycle_actions or [], "boot")
        ):
            adapter = self._adapter_registry.get_current(pack_id)
            pack_release = getattr(adapter, "pack_release", "") if adapter is not None else ""
            result = await adapter_lifecycle_action(
                adapter_registry=self._adapter_registry,
                pack_id=pack_id,
                pack_release=pack_release,
                host_id="",
                identity_value=connection_target,
                action="boot",
                args={"headless": headless},
            )
            if result and result.get("state") and result.get("state") not in {"booting", "booted"}:
                resolved_connection_target = str(result["state"])
            elif result and result.get("success") is False:
                raise DeviceNotFoundError(str(result.get("detail") or f"{connection_target!r} could not be booted"))
        merged_extra_caps = dict(extra_caps) if extra_caps else {}
        if self._adapter_registry is not None:
            adapter = self._adapter_registry.get_current(pack_id)
            if adapter is not None:
                pack_release = getattr(adapter, "pack_release", "")
                adapter_caps = await adapter_pre_session(
                    adapter_registry=self._adapter_registry,
                    pack_id=pack_id,
                    pack_release=pack_release,
                    platform_id=platform_id,
                    identity_value=resolved_connection_target,
                    capabilities=merged_extra_caps,
                )
                merged_extra_caps.update(adapter_caps)
        spec = AppiumLaunchSpec(
            connection_target=resolved_connection_target,
            port=port,
            plugins=list(plugins) if plugins else None,
            extra_caps=merged_extra_caps if merged_extra_caps else None,
            stereotype_caps=dict(stereotype_caps) if stereotype_caps else None,
            accepting_new_sessions=accepting_new_sessions,
            stop_pending=stop_pending,
            grid_run_id=grid_run_id,
            session_override=session_override,
            device_type=device_type,
            ip_address=ip_address,
            manage_grid_node=manage_grid_node,
            pack_id=pack_id,
            platform_id=platform_id,
            appium_platform_name=appium_platform_name,
            workaround_env=dict(workaround_env) if workaround_env else None,
            insecure_features=list(insecure_features) if insecure_features else [],
            grid_slots=list(grid_slots) if grid_slots else ["native"],
            lifecycle_actions=list(lifecycle_actions) if lifecycle_actions else [],
            connection_behavior=dict(connection_behavior) if connection_behavior else {},
            headless=headless,
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

            self._launch_specs[port] = spec
            self._intentional_stop_ports.discard(port)
            self._stop_pending_ports.discard(port)
            appium_proc = await self._start_appium_server(spec, clear_logs_on_failure=port not in self._info)

            if manage_grid_node:
                try:
                    await self._start_grid_node_service(spec)
                except Exception:
                    await self._cleanup_started_appium_after_grid_node_failure(spec.port, appium_proc)
                    raise

            info = self._info.get(port)
            if info is None:
                info = AppiumProcessInfo(
                    port=port,
                    pid=appium_proc.pid,
                    connection_target=resolved_connection_target,
                    platform_id=platform_id,
                )
                self._info[port] = info
            else:
                info.pid = appium_proc.pid
                info.connection_target = resolved_connection_target
                info.platform_id = platform_id
            return info

    async def _start_grid_node_service(self, spec: AppiumLaunchSpec) -> GridNodeSupervisorHandle:
        existing = self._grid_supervisors.get(spec.port)
        if existing is not None and existing.is_running():
            return existing

        appium_platform = spec.appium_platform_name or spec.platform_id
        caps: dict[str, Any] = {"appium:udid": spec.connection_target, "platformName": appium_platform}
        if spec.stereotype_caps:
            caps.update(spec.stereotype_caps)
        elif spec.extra_caps:
            caps.update(spec.extra_caps)
        caps["gridfleet:run_id"] = str(spec.grid_run_id) if spec.grid_run_id is not None else "free"
        caps["gridfleet:available"] = spec.accepting_new_sessions

        node_port = self._allocate_node_port()
        # Selenium hub deserializes nodeId via UUID.fromString — derive a stable UUID
        # from the device target so identity survives agent restarts.
        node_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gridfleet-node:{spec.connection_target}:{spec.port}"))
        config = GridNodeConfig(
            node_id=node_uuid,
            node_uri=self._grid_external_url(node_port),
            appium_upstream=f"http://127.0.0.1:{spec.port}",
            slots=build_slots(base_caps=caps, grid_slots=spec.grid_slots),
            hub_publish_url=agent_settings.grid_publish_url,
            hub_subscribe_url=agent_settings.grid_subscribe_url,
            heartbeat_sec=getattr(agent_settings, "grid_node_heartbeat_sec", 5.0),
            session_timeout_sec=getattr(agent_settings, "grid_node_session_timeout_sec", 300.0),
            proxy_timeout_sec=getattr(agent_settings, "grid_node_proxy_timeout_sec", 60.0),
            bind_host=getattr(agent_settings, "grid_node_bind_host", "0.0.0.0"),
        )

        def factory() -> GridNodeService:
            # `hub_publish_url` / `hub_subscribe_url` are bus-centric (Selenium
            # `--publish-events` / `--subscribe-events` semantics). The node's PUB socket
            # must connect to the bus's XSUB endpoint, and the node's SUB to the bus's
            # XPUB endpoint, so the URLs are swapped at the EventBus boundary.
            bus = EventBus(
                publish_url=config.hub_subscribe_url,
                subscribe_url=config.hub_publish_url,
                heartbeat_sec=config.heartbeat_sec,
            )
            return GridNodeService(config=config, bus=bus)

        handle = start_grid_node_supervisor(factory=factory, config=config)
        try:
            await handle.start()
            await handle.wait_until_running()
        except Exception:
            with contextlib.suppress(Exception):
                await handle.stop()
            raise
        self._grid_supervisors[spec.port] = handle
        return handle

    async def reconfigure(
        self,
        port: int,
        *,
        accepting_new_sessions: bool,
        stop_pending: bool,
        grid_run_id: uuid.UUID | None,
    ) -> None:
        handle = self._grid_supervisors.get(port)
        if handle is None or handle.service is None:
            raise DeviceNotFoundError(f"No running grid node for Appium port {port}")
        if port not in self._info:
            raise DeviceNotFoundError(f"No managed Appium process is running on port {port}")

        service = handle.service
        caps = service.slot_stereotype_caps()
        caps["gridfleet:available"] = accepting_new_sessions
        caps["gridfleet:run_id"] = str(grid_run_id) if grid_run_id is not None else "free"
        await service.reregister_with_stereotype(new_caps=caps)

        spec = self._launch_specs.get(port)
        if spec is not None:
            self._launch_specs[port] = AppiumLaunchSpec(
                connection_target=spec.connection_target,
                port=spec.port,
                plugins=spec.plugins,
                extra_caps=spec.extra_caps,
                stereotype_caps=spec.stereotype_caps,
                accepting_new_sessions=accepting_new_sessions,
                stop_pending=stop_pending,
                grid_run_id=grid_run_id,
                session_override=spec.session_override,
                device_type=spec.device_type,
                ip_address=spec.ip_address,
                manage_grid_node=spec.manage_grid_node,
                pack_id=spec.pack_id,
                platform_id=spec.platform_id,
                appium_platform_name=spec.appium_platform_name,
                workaround_env=spec.workaround_env,
                insecure_features=list(spec.insecure_features),
                grid_slots=list(spec.grid_slots),
                lifecycle_actions=list(spec.lifecycle_actions),
                connection_behavior=dict(spec.connection_behavior),
                headless=spec.headless,
            )

        if stop_pending:
            self._stop_pending_ports.add(port)
            if service.has_active_session():
                self._ensure_stop_when_grid_idle_task(port)
                return
            await self.stop(port)
            return
        self._stop_pending_ports.discard(port)
        self._cancel_task(self._stop_pending_tasks, port)

    def _ensure_stop_when_grid_idle_task(self, port: int) -> None:
        existing = self._stop_pending_tasks.get(port)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._stop_when_grid_idle(port))
        self._register_port_task(self._stop_pending_tasks, port, task)

    async def _stop_when_grid_idle(self, port: int) -> None:
        while port in self._stop_pending_ports:
            handle = self._grid_supervisors.get(port)
            service = handle.service if handle is not None else None
            if service is None or not service.has_active_session():
                await self.stop(port)
                return
            await asyncio.sleep(1)

    async def _cleanup_started_appium_after_grid_node_failure(
        self, port: int, appium_proc: asyncio.subprocess.Process
    ) -> None:
        self._intentional_stop_ports.add(port)
        self._cancel_task(self._appium_restart_tasks, port)
        self._cancel_task(self._appium_watch_tasks, port)
        self._grid_supervisors.pop(port, None)
        self._appium_procs.pop(port, None)
        self._info.pop(port, None)
        self._launch_specs.pop(port, None)
        self._appium_restart_attempts.pop(port, None)
        self._appium_restart_backoff_steps.pop(port, None)
        self._stop_pending_ports.discard(port)
        self._cancel_task(self._stop_pending_tasks, port)
        if appium_proc.returncode is None:
            appium_proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(appium_proc.wait(), timeout=STOP_GRACE_PERIOD)
            except TimeoutError:
                appium_proc.kill()
                await appium_proc.wait()
        for task in self._log_tasks.pop(port, []):
            task.cancel()
        # Intentionally do NOT discard `_intentional_stop_ports` here. Cleanup
        # represents permanent grid-node startup failure: any straggler exit
        # handler must NOT trigger an auto-restart against the now-cleared
        # launch_spec. The next explicit `start()` for this port resets the
        # flag at line 838.

    async def _stop_grid_node_service(self, port: int) -> None:
        handle = self._grid_supervisors.pop(port, None)
        if handle is not None:
            await handle.stop()

    async def _drop_failed_managed_port(self, port: int) -> None:
        """Forget stale ownership for a crashed Appium process without touching an unmanaged listener.

        A grid-node stop failure must not abort cleanup — the Appium process and
        port metadata still need to be released so the host can recover the
        port. Suppress and log instead.
        """
        self._cancel_task(self._appium_restart_tasks, port)
        self._cancel_task(self._appium_watch_tasks, port)
        try:
            await self._stop_grid_node_service(port)
        except Exception as exc:
            logger.warning(
                "grid node stop failed during managed-port cleanup on port %s: %s",
                sanitize_log_value(port),
                sanitize_log_value(exc),
            )
        self._appium_procs.pop(port, None)
        self._info.pop(port, None)
        self._launch_specs.pop(port, None)
        self._appium_restart_attempts.pop(port, None)
        self._appium_restart_backoff_steps.pop(port, None)

    async def stop(self, port: int) -> None:
        async with self._start_lock:
            self._intentional_stop_ports.add(port)
            self._cancel_task(self._appium_restart_tasks, port)
            self._cancel_task(self._appium_watch_tasks, port)
            current = asyncio.current_task()
            stop_pending_task = self._stop_pending_tasks.get(port)
            if stop_pending_task is not None and stop_pending_task is not current:
                self._cancel_task(self._stop_pending_tasks, port)

            # Stop Grid Node first. A grid-node failure must not leak the
            # Appium process; suppress and log so we always reach the Appium
            # teardown below.
            try:
                await self._stop_grid_node_service(port)
            except Exception as exc:
                logger.warning(
                    "grid node stop failed for port %s: %s",
                    sanitize_log_value(port),
                    sanitize_log_value(exc),
                )

            # Stop Appium
            appium_proc = self._appium_procs.pop(port, None)
            self._info.pop(port, None)
            self._launch_specs.pop(port, None)
            self._appium_restart_attempts.pop(port, None)
            self._appium_restart_backoff_steps.pop(port, None)

            if appium_proc and appium_proc.returncode is None:
                appium_proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(appium_proc.wait(), timeout=STOP_GRACE_PERIOD)
                except TimeoutError:
                    appium_proc.kill()
                    await appium_proc.wait()

            for t in self._log_tasks.pop(port, []):
                t.cancel()
            self._intentional_stop_ports.discard(port)

    def require_managed_running_port(self, port: int) -> None:
        proc = self._appium_procs.get(port)
        if proc is None or proc.returncode is not None:
            raise DeviceNotFoundError(f"No managed Appium process is running on port {port}")

    def loopback_origin_for_managed_port(self, port: int) -> httpx.URL:
        self.require_managed_running_port(port)
        return _loopback_appium_origin(port)

    async def status(self, port: int) -> dict[str, Any]:
        proc = self._appium_procs.get(port)
        if proc is None or proc.returncode is not None:
            return {"running": False, "port": port}

        appium_status = await self._fetch_appium_status(port)
        if appium_status is None:
            return {"running": False, "port": port}
        return {"running": True, "port": port, "pid": proc.pid, "appium_status": appium_status}

    def get_logs(self, port: int, lines: int = 100) -> list[str]:
        buf = self._logs.get(port)
        if buf is None:
            return []
        all_lines = list(buf)
        return all_lines[-lines:]

    def list_running(self) -> list[AppiumProcessInfo]:
        running: list[AppiumProcessInfo] = []
        for port, info in self._info.items():
            proc = self._appium_procs.get(port)
            if proc is not None and proc.returncode is None:
                running.append(info)
        return running

    def process_snapshot(self) -> dict[str, Any]:
        return {
            "running_nodes": [self._running_node_snapshot(info) for info in self.list_running()],
            "recent_restart_events": [event.to_payload() for event in self._recent_restart_events],
        }

    def _running_node_snapshot(self, info: AppiumProcessInfo) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "port": info.port,
            "pid": info.pid,
            "connection_target": info.connection_target,
            "platform_id": info.platform_id,
        }
        supervisor = self._grid_supervisors.get(info.port)
        if supervisor is not None:
            supervisor_snapshot = supervisor.snapshot()
            status = supervisor_snapshot.get("status")
            if status is not None:
                payload["grid_node_status"] = status
        return payload

    async def shutdown(self) -> None:
        ports = sorted(set(self._appium_procs) | set(self._grid_supervisors) | set(self._launch_specs))
        for port in ports:
            with contextlib.suppress(Exception):
                await self.stop(port)
        for task_map in (self._appium_restart_tasks, self._appium_watch_tasks, self._stop_pending_tasks):
            for task in task_map.values():
                task.cancel()
            task_map.clear()
        # If a per-port `stop()` raised before reaching its `_log_tasks.pop`,
        # the log-streamer tasks survive shutdown and leak into the next
        # event-loop tick. Cancel anything still tracked here.
        for port, tasks in list(self._log_tasks.items()):
            for task in tasks:
                task.cancel()
            self._log_tasks.pop(port, None)

    async def _fetch_appium_status(self, port: int) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(base_url=_loopback_appium_origin(port)) as client:
                resp = await client.get("/status", timeout=2)
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

    async def _wait_for_readiness(self, port: int, process: asyncio.subprocess.Process) -> bool:
        deadline = asyncio.get_event_loop().time() + READINESS_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if process.returncode is not None:
                return False
            if await self._can_connect_to_appium(port):
                return True
            await asyncio.sleep(READINESS_POLL_INTERVAL)
        return False


def _get_network_devices(mgr: "AppiumProcessManager | None" = None) -> list[dict[str, Any]]:
    """Return network devices from currently running Appium processes.

    Defaults to the module-level ``appium_mgr`` singleton when ``mgr`` is
    omitted, for parity with the previous module-level helper that lived
    in ``main.py``.
    """
    from agent_app.appium import appium_mgr  # noqa: PLC0415 - local import avoids cycle

    manager = mgr or appium_mgr
    devices: list[dict[str, Any]] = []
    for info in manager.list_running():
        if re.match(r"\d+\.\d+\.\d+\.\d+:\d+", info.connection_target):
            ip, _, port_str = info.connection_target.rpartition(":")
            devices.append({"connection_target": info.connection_target, "ip_address": ip, "port": int(port_str)})
    return devices
