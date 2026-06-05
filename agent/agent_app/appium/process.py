import asyncio
import collections
import contextlib
import inspect
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from agent_app import http_client
from agent_app.appium.exceptions import (
    AlreadyRunningError,
    DeviceNotFoundError,
    InvalidStartPayloadError,
    PortOccupiedError,
    RuntimeMissingError,
    RuntimeNotInstalledError,
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
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import SubprocessEnvContribution
from agent_app.pack.dispatch import adapter_lifecycle_action, adapter_pre_session
from agent_app.pack.runtime_registry import RuntimeRegistry

logger = logging.getLogger(__name__)


def _has_lifecycle_action(actions: list[dict[str, Any]], action_id: str) -> bool:
    """Return True if any action in *actions* has id == *action_id*."""
    return any(action.get("id") == action_id for action in actions)


READINESS_TIMEOUT = 30
READINESS_POLL_INTERVAL = 1
STOP_GRACE_PERIOD = 5
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
    *,
    appium_bin: str | None = None,
    appium_home: str | None = None,
    workaround_env: dict[str, str] | None = None,
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
        # setdefault: host env wins; workaround_env overrides later via update()
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
    session_override: bool
    device_type: str | None
    ip_address: str | None
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
        self._intentional_stop_ports: set[int] = set()
        self._runtime_registry: RuntimeRegistry | None = None
        self._adapter_registry: AdapterRegistry | None = None
        self._start_lock = asyncio.Lock()

    def set_runtime_registry(self, registry: RuntimeRegistry) -> None:
        self._runtime_registry = registry

    def set_adapter_registry(self, registry: AdapterRegistry) -> None:
        self._adapter_registry = registry

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
            plugins=spec.plugins,
            extra_caps=spec.extra_caps,
            accepting_new_sessions=spec.accepting_new_sessions,
            stop_pending=spec.stop_pending,
            grid_run_id=spec.grid_run_id,
            session_override=spec.session_override,
            device_type=spec.device_type,
            ip_address=spec.ip_address,
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

        caps = {"platformName": appium_platform}
        if extra_caps:
            caps.update(extra_caps)
        caps = sanitize_appium_driver_capabilities(caps)

        invocation = resolve_appium_invocation_for_pack(pack_id=spec.pack_id, registry=self._runtime_registry)
        adapter = self._adapter_registry.get_current(spec.pack_id) if self._adapter_registry is not None else None
        adapter_env = None
        if adapter is not None and hasattr(adapter, "subprocess_env"):
            adapter_env = adapter.subprocess_env()
            if inspect.isawaitable(adapter_env):
                adapter_env = await adapter_env
        env = _build_env(
            appium_bin=invocation.binary,
            appium_home=invocation.env_extra.get("APPIUM_HOME"),
            workaround_env=spec.workaround_env,
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
        if spec.plugins:
            appium_cmd.extend(["--use-plugins", ",".join(spec.plugins)])
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
        plugins: list[str] | None = None,
        extra_caps: dict[str, Any] | None = None,
        accepting_new_sessions: bool = True,
        stop_pending: bool = False,
        grid_run_id: uuid.UUID | None = None,
        session_override: bool = True,
        device_type: str | None = None,
        ip_address: str | None = None,
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
        if self._adapter_registry is not None and _has_lifecycle_action(lifecycle_actions or [], "boot"):
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
            if result and result.get("resolved_connection_target"):
                resolved_connection_target = str(result["resolved_connection_target"])
            elif result and result.get("success") is False:
                raise DeviceNotFoundError(str(result.get("detail") or f"{connection_target!r} could not be started"))
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
            accepting_new_sessions=accepting_new_sessions,
            stop_pending=stop_pending,
            grid_run_id=grid_run_id,
            session_override=session_override,
            device_type=device_type,
            ip_address=ip_address,
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
            appium_proc = await self._start_appium_server(spec, clear_logs_on_failure=port not in self._info)

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
            self._launch_specs[port] = AppiumLaunchSpec(
                connection_target=spec.connection_target,
                port=spec.port,
                plugins=spec.plugins,
                extra_caps=spec.extra_caps,
                accepting_new_sessions=accepting_new_sessions,
                stop_pending=stop_pending,
                grid_run_id=grid_run_id,
                session_override=spec.session_override,
                device_type=spec.device_type,
                ip_address=spec.ip_address,
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
            # Track the port so ``_auto_restart_appium`` skips resurrection if
            # the Appium process exits while a stop is pending. The actual
            # stop is owned by the backend's appium reconciler, which
            # converges based on ``AppiumNode.desired_state``.
            self._stop_pending_ports.add(port)
        else:
            self._stop_pending_ports.discard(port)

    async def _drop_failed_managed_port(self, port: int) -> None:
        """Forget stale ownership for a crashed Appium process.

        Release the Appium process and port metadata so the host can recover
        the port.
        """
        self._cancel_task(self._appium_restart_tasks, port)
        self._cancel_task(self._appium_watch_tasks, port)
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

            appium_log_path(port).unlink(missing_ok=True)
            self._intentional_stop_ports.discard(port)

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

    def process_snapshot(self) -> dict[str, Any]:
        return {
            "running_nodes": [self._running_node_snapshot(info) for info in self.list_running()],
            "recent_restart_events": [event.to_payload() for event in self._recent_restart_events],
        }

    def _running_node_snapshot(self, info: AppiumProcessInfo) -> dict[str, Any]:
        return {
            "port": info.port,
            "pid": info.pid,
            "connection_target": info.connection_target,
            "platform_id": info.platform_id,
        }

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
