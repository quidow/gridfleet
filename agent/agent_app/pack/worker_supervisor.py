"""Supervise one out-of-process adapter worker per pack release."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import agent_app.pack.worker_protocol as wp
from agent_app.pack.adapter_dispatch import (
    ADAPTER_HOOK_TIMEOUT_SECONDS,
    AdapterContractError,
    AdapterHookExecutionError,
    AdapterHookTimeoutError,
)
from agent_app.pack.adapter_types import SubprocessEnvContribution

if TYPE_CHECKING:
    from pathlib import Path

KILL_GRACE_SEC = 5.0
RESTART_DELAYS_SEC = (1, 2, 4, 8, 16, 30)

logger = logging.getLogger(__name__)


@dataclass
class _PendingRequest:
    hook: str
    future: asyncio.Future[Any]


class WorkerHandle:
    """A typed client for one supervised pack worker."""

    def __init__(
        self,
        supervisor: WorkerSupervisor,
        pack_id: str,
        release: str,
        site_dir: Path,
    ) -> None:
        self.pack_id = pack_id
        self.release = release
        self.supported_hooks: frozenset[str] = frozenset()
        self.subprocess_env = SubprocessEnvContribution()
        self.tool_versions: dict[str, str | None] = {}
        self._supervisor = supervisor
        self._site_dir = site_dir
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._next_id = 1
        self._alive = False
        self._shutting_down = False

    @property
    def alive(self) -> bool:
        return self._alive

    async def call(self, hook: str, payload: dict[str, Any]) -> Any:  # noqa: ANN401
        if not self._alive or self._process is None or self._process.stdin is None:
            raise AdapterHookExecutionError(
                hook,
                self.pack_id,
                self.release,
                RuntimeError("worker exited"),
            )
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingRequest(hook, future)
        try:
            self._process.stdin.write(wp.encode_request(request_id, hook, payload).encode() + b"\n")
            await self._process.stdin.drain()
            response = await asyncio.wait_for(
                future,
                timeout=self._supervisor._hook_timeout + self._supervisor._kill_grace_sec,
            )
        except TimeoutError:
            self._pending.pop(request_id, None)
            await self._supervisor._kill_for_timeout(self)
            raise AdapterHookTimeoutError(hook, self.pack_id, self.release) from None
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            await self._supervisor._handle_failure(self, RuntimeError("worker exited"))
            raise AdapterHookExecutionError(hook, self.pack_id, self.release, exc) from exc
        finally:
            self._pending.pop(request_id, None)

        error = response.get("error")
        if not response.get("ok"):
            kind = error.get("kind") if isinstance(error, dict) else "unknown"
            message = error.get("message", "worker hook failed") if isinstance(error, dict) else str(error)
            if kind == "timeout":
                raise AdapterHookTimeoutError(hook, self.pack_id, self.release)
            raise AdapterHookExecutionError(hook, self.pack_id, self.release, RuntimeError(message))

        spec = wp.HOOK_SPECS.get(hook)
        if spec is None:
            raise AdapterContractError(hook, self.pack_id, self.release, "unknown hook")
        try:
            result = spec.decode_result(response.get("result"))
        except Exception as exc:
            raise AdapterContractError(hook, self.pack_id, self.release, str(exc)) from exc
        if not isinstance(result, spec.expected):
            raise AdapterContractError(
                hook,
                self.pack_id,
                self.release,
                f"expected {spec.expected.__name__}, got {type(result).__name__}",
            )
        return result

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            await asyncio.gather(self._restart_task, return_exceptions=True)
            self._restart_task = None
        self._alive = False
        self._fail_pending(RuntimeError("worker shut down"))
        await self._supervisor._stop_process(self)
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None

    def _fail_pending(self, cause: Exception) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(AdapterHookExecutionError(pending.hook, self.pack_id, self.release, cause))
        self._pending.clear()


class WorkerSupervisor:
    """Spawn, contain, and restart pack workers."""

    def __init__(
        self,
        *,
        hook_timeout: float = ADAPTER_HOOK_TIMEOUT_SECONDS,
        handshake_timeout: float = 30.0,
        kill_grace_sec: float = KILL_GRACE_SEC,
        restart_delays: tuple[float, ...] = RESTART_DELAYS_SEC,
    ) -> None:
        self._hook_timeout = hook_timeout
        self._handshake_timeout = handshake_timeout
        self._kill_grace_sec = kill_grace_sec
        self._restart_delays = restart_delays
        self._handles: dict[tuple[str, str], WorkerHandle] = {}

    async def start(self, pack_id: str, release: str, site_dir: Path) -> WorkerHandle:
        key = (pack_id, release)
        existing = self._handles.get(key)
        if existing is not None:
            if existing.alive:
                return existing
            await existing.shutdown()
        handle = WorkerHandle(self, pack_id, release, site_dir)
        self._handles[key] = handle
        try:
            await self._spawn(handle)
        except Exception:
            self._handles.pop(key, None)
            await handle.shutdown()
            raise
        return handle

    async def _spawn(self, handle: WorkerHandle) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_app.pack.worker",
            "--pack-id",
            handle.pack_id,
            "--release",
            handle.release,
            "--site",
            str(handle._site_dir),
            "--hook-timeout",
            str(self._hook_timeout),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        handle._process = proc
        if proc.stdout is None:
            raise RuntimeError("worker stdout is unavailable")
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._handshake_timeout)
            if not line:
                raise RuntimeError("worker exited before handshake")
            handshake = wp.decode_line(line.decode())
            if handshake.get("protocol_version", wp.PROTOCOL_VERSION) != wp.PROTOCOL_VERSION:
                raise RuntimeError("unsupported worker protocol version")
            handle.supported_hooks = frozenset(handshake["supported_hooks"])
            handle.subprocess_env = SubprocessEnvContribution(**handshake.get("subprocess_env", {}))
            handle.tool_versions = dict(handshake.get("tool_versions", {}))
        except Exception:
            await self._stop_process(handle)
            raise
        handle._alive = True
        handle._reader_task = asyncio.create_task(self._read_responses(handle, proc))

    async def _read_responses(self, handle: WorkerHandle, proc: asyncio.subprocess.Process) -> None:
        try:
            assert proc.stdout is not None
            while line := await proc.stdout.readline():
                response = wp.decode_line(line.decode())
                request_id = response.get("id")
                if not isinstance(request_id, int):
                    continue
                pending = handle._pending.get(request_id)
                if pending is not None and not pending.future.done():
                    pending.future.set_result(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("pack worker response reader failed for %s@%s", handle.pack_id, handle.release)
            await self._handle_failure(handle, exc)
            return
        if not handle._shutting_down and handle._alive:
            await self._handle_failure(handle, RuntimeError("worker exited"))

    async def _handle_failure(self, handle: WorkerHandle, cause: Exception) -> None:
        if handle._shutting_down or not handle._alive:
            return
        handle._alive = False
        handle._fail_pending(cause)
        await self._stop_process(handle)
        self._schedule_restart(handle)

    async def _kill_for_timeout(self, handle: WorkerHandle) -> None:
        if not handle._alive:
            return
        handle._alive = False
        handle._fail_pending(RuntimeError("worker killed after hook deadline"))
        await self._stop_process(handle)
        self._schedule_restart(handle)

    def _schedule_restart(self, handle: WorkerHandle) -> None:
        if handle._shutting_down:
            return
        if handle._restart_task is None or handle._restart_task.done():
            handle._restart_task = asyncio.create_task(self._restart(handle))

    async def _restart(self, handle: WorkerHandle) -> None:
        for delay in self._restart_delays:
            await asyncio.sleep(delay)
            if handle._shutting_down:
                return
            try:
                await self._spawn(handle)
            except Exception:
                logger.exception("pack worker restart failed for %s@%s", handle.pack_id, handle.release)
                handle._alive = False
                continue
            return

    async def _stop_process(self, handle: WorkerHandle) -> None:
        proc = handle._process
        handle._process = None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._kill_grace_sec)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    async def shutdown_all(self) -> None:
        handles = list(self._handles.values())
        await asyncio.gather(*(handle.shutdown() for handle in handles), return_exceptions=False)
        self._handles.clear()
