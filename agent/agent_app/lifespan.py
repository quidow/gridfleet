"""FastAPI lifespan and its supporting helpers.

Extracted from ``agent_app/main.py``. Owns the long-running background
tasks (capabilities refresh, registration, pack state loop) plus the
adapter loader used by the pack state loop, plus the manager-auth
helper and pack-state HTTP client.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx

from agent_app import observability as agent_observability
from agent_app.appium import appium_mgr
from agent_app.config import agent_settings, secret_value
from agent_app.host.capabilities import capabilities_refresh_loop, refresh_capabilities_snapshot
from agent_app.http_client import close as close_shared_http_client
from agent_app.http_client import get_client as get_shared_http_client
from agent_app.logs.shipper import LogShipperTask
from agent_app.pack.adapter_loader import load_adapter
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.runtime import AppiumRuntimeManager
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.sidecar_supervisor import SidecarSupervisor
from agent_app.pack.state import PackStateClient, PackStateLoop
from agent_app.pack.tarball_fetch import download_and_verify
from agent_app.pack.version_catalog import NpmVersionCatalog
from agent_app.registration import registration_loop

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from fastapi import FastAPI

    from agent_app.appium.process import AppiumProcessManager
    from agent_app.grid_node.supervisor import GridNodeSupervisorHandle
    from agent_app.pack.manifest import DesiredPack
    from agent_app.pack.runtime import RuntimeEnv
    from agent_app.pack.state import AdapterLoaderFn

logger = logging.getLogger(__name__)
GRID_NODE_SHUTDOWN_TIMEOUT_SEC = 10.0
BOOT_ID = uuid4()


def _watchdog(
    name: str,
    restart: Callable[[], asyncio.Task[None]] | None = None,
) -> Callable[[asyncio.Task[Any]], None]:
    """Return a done_callback that logs unhandled exceptions from supervised tasks."""

    def _cb(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            logger.info("background task %s exited cleanly", name)
            return
        logger.error(
            "background task %s crashed",
            name,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if restart is not None:
            logger.info("restarting background task %s", name)
            restart()

    return _cb


def _manager_auth() -> httpx.BasicAuth | None:
    username = agent_settings.manager.manager_auth_username
    password = secret_value(agent_settings.manager.manager_auth_password)
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


class HttpPackStateClient(PackStateClient):
    def __init__(self, base_url: str, host_id: str) -> None:
        self._base = base_url.rstrip("/")
        self._host_id = host_id

    async def fetch_desired(self) -> dict[str, Any]:
        client = get_shared_http_client()
        kwargs: dict[str, Any] = {
            "params": {"host_id": self._host_id},
            "timeout": 15.0,
        }
        if (auth := _manager_auth()) is not None:
            kwargs["auth"] = auth
        resp = await client.get(f"{self._base}/agent/driver-packs/desired", **kwargs)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def post_status(self, payload: dict[str, Any]) -> None:
        client = get_shared_http_client()
        kwargs: dict[str, Any] = {"json": payload, "timeout": 15.0}
        if (auth := _manager_auth()) is not None:
            kwargs["auth"] = auth
        resp = await client.post(f"{self._base}/agent/driver-packs/status", **kwargs)
        resp.raise_for_status()


def _build_adapter_loader(
    backend_url: str,
    adapter_registry: AdapterRegistry,
) -> AdapterLoaderFn:
    """Return an ``AdapterLoaderFn`` that fetches a pack tarball and loads its adapter."""

    base = backend_url.rstrip("/")

    async def _load(pack: DesiredPack, env: RuntimeEnv) -> None:
        if not pack.tarball_sha256:
            return
        runtime_dir = Path(env.appium_home)
        tarball_dir = runtime_dir / "tarballs"
        client = get_shared_http_client()
        tarball_path = await download_and_verify(
            client=client,
            pack_id=pack.id,
            release=pack.release,
            expected_sha256=pack.tarball_sha256,
            dest_dir=tarball_dir,
            base_url=base,
            auth=_manager_auth(),
            timeout=60.0,
        )
        adapter = await load_adapter(
            pack_id=pack.id,
            release=pack.release,
            tarball_path=tarball_path,
            runtime_dir=runtime_dir,
        )
        adapter_registry.set(pack.id, pack.release, adapter)

    return _load


async def _start_pack_loop_when_ready(
    app: FastAPI,
    host_identity: HostIdentity,
    backend_url: str,
    runtime_registry: RuntimeRegistry,
    adapter_registry: AdapterRegistry,
    sidecar_supervisor: SidecarSupervisor,
) -> None:
    host_id = await host_identity.wait()
    app.state.pack_state_loop_enabled = True
    client = HttpPackStateClient(backend_url, host_id)
    runtime_mgr = AppiumRuntimeManager()
    adapter_loader = _build_adapter_loader(backend_url, adapter_registry)
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_id=host_id,
        runtime_registry=runtime_registry,
        adapter_registry=adapter_registry,
        adapter_loader=adapter_loader,
        sidecar_supervisor=sidecar_supervisor,
        version_catalog=NpmVersionCatalog(),
    )
    app.state.pack_state_loop = loop
    await loop.run_forever()


async def _start_log_shipper_when_ready(
    host_identity: HostIdentity,
    backend_url: str,
    *,
    boot_id: UUID = BOOT_ID,
) -> None:
    if agent_observability.shipper_queue is None:
        return
    host_id_raw = await host_identity.wait()
    try:
        host_id = UUID(host_id_raw)
    except ValueError:
        logger.warning("log shipper disabled because host_id is not a UUID: %s", host_id_raw)
        return
    shipper = LogShipperTask(
        client=get_shared_http_client(),
        host_id=host_id,
        boot_id=boot_id,
        queue=agent_observability.shipper_queue,
        base_url=backend_url,
        auth=_manager_auth(),
    )
    await shipper.run()


async def _stop_grid_node_supervisors_for_shutdown(
    manager: AppiumProcessManager,
    *,
    timeout_sec: float = GRID_NODE_SHUTDOWN_TIMEOUT_SEC,
) -> None:
    supervisors = manager.iter_grid_supervisors()
    if not supervisors:
        return

    async def _stop_one(port: int, supervisor: GridNodeSupervisorHandle) -> int:
        await supervisor.stop()
        return port

    tasks = [asyncio.create_task(_stop_one(port, supervisor)) for port, supervisor in supervisors]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_sec)
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Leave timed-out supervisor handles registered so the immediate
        # `appium_mgr.shutdown()` can re-attempt the stop.
        # `GridNodeSupervisorHandle.stop()` is idempotent (cancelled task is a
        # no-op on the second pass), so retrying is safe and orphans nothing.
        logger.warning("timed out stopping grid node supervisors during shutdown")
        return
    for result in results:
        if isinstance(result, int):
            manager.pop_grid_supervisor(result)
        elif isinstance(result, Exception):
            logger.warning("failed to stop grid node supervisor during shutdown", exc_info=result)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await refresh_capabilities_snapshot()
    capabilities_task = asyncio.create_task(capabilities_refresh_loop(refresh_immediately=False))
    capabilities_task.add_done_callback(_watchdog("capabilities_refresh"))

    host_identity = HostIdentity()
    runtime_registry = RuntimeRegistry()
    adapter_registry = AdapterRegistry()
    sidecar_supervisor = SidecarSupervisor()
    app.state.host_identity = host_identity
    app.state.runtime_registry = runtime_registry
    app.state.adapter_registry = adapter_registry
    app.state.sidecar_supervisor = sidecar_supervisor
    app.state.pack_state_loop_enabled = False
    app.state.pack_state_loop = None

    env_host_id = agent_settings.core.host_id
    backend_url = agent_settings.manager.effective_backend_url
    if env_host_id:
        host_identity.set(env_host_id)
        app.state.pack_state_loop_enabled = True

    reg_task: asyncio.Task[None]

    def _start_registration_task() -> asyncio.Task[None]:
        nonlocal reg_task
        reg_task = asyncio.create_task(
            registration_loop(
                agent_settings.manager.manager_url,
                agent_settings.core.agent_port,
                host_identity,
            )
        )
        reg_task.add_done_callback(_watchdog("registration", _start_registration_task))
        return reg_task

    reg_task = _start_registration_task()
    appium_mgr.set_runtime_registry(runtime_registry)
    appium_mgr.set_adapter_registry(adapter_registry)

    pack_task: asyncio.Task[None] | None = None
    log_shipper_task: asyncio.Task[None] | None = None
    if backend_url:
        pack_task = asyncio.create_task(
            _start_pack_loop_when_ready(
                app, host_identity, backend_url, runtime_registry, adapter_registry, sidecar_supervisor
            )
        )
        pack_task.add_done_callback(_watchdog("pack_state_loop"))
        log_shipper_task = asyncio.create_task(_start_log_shipper_when_ready(host_identity, backend_url))
        log_shipper_task.add_done_callback(_watchdog("log_shipper"))

    try:
        yield
    finally:
        if pack_task is not None:
            pack_task.cancel()
        if log_shipper_task is not None:
            log_shipper_task.cancel()
        reg_task.cancel()
        capabilities_task.cancel()
        await _stop_grid_node_supervisors_for_shutdown(appium_mgr)
        await appium_mgr.shutdown()
        await sidecar_supervisor.shutdown()
        await close_shared_http_client()
