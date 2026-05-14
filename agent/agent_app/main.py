import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from agent_app.api_auth import BasicAuthMiddleware
from agent_app.appium import appium_mgr  # re-exported for tests pending patch-target migration
from agent_app.appium.process import AppiumProcessManager
from agent_app.appium.router import router as appium_router
from agent_app.appium.schemas import (
    AppiumStartRequest as AppiumStartRequest,
)  # re-exported for tests pending patch-target migration
from agent_app.config import agent_settings
from agent_app.error_codes import AgentErrorCode, http_exc
from agent_app.grid_node.supervisor import GridNodeServiceProtocol, GridNodeSupervisorHandle
from agent_app.host.capabilities import (
    capabilities_refresh_loop,
    refresh_capabilities_snapshot,
)
from agent_app.host.router import router as host_router
from agent_app.http_client import close as close_shared_http_client
from agent_app.http_client import get_client as get_shared_http_client
from agent_app.observability import RequestContextMiddleware, configure_logging
from agent_app.pack.adapter_loader import load_adapter
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.manifest import DesiredPack
from agent_app.pack.router import router as pack_router
from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.sidecar_supervisor import SidecarSupervisor
from agent_app.pack.state import AdapterLoaderFn, PackStateClient, PackStateLoop
from agent_app.pack.tarball_fetch import download_and_verify
from agent_app.pack.version_catalog import NpmVersionCatalog
from agent_app.plugins.router import router as plugins_router
from agent_app.registration import registration_loop
from agent_app.terminal.router import router as terminal_router
from agent_app.tools.router import router as tools_router

configure_logging()
logger = logging.getLogger(__name__)

GRID_NODE_SHUTDOWN_TIMEOUT_SEC = 10.0


def _manager_auth() -> httpx.BasicAuth | None:
    username = agent_settings.manager_auth_username
    password = agent_settings.manager_auth_password
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
        async with httpx.AsyncClient(base_url=base, timeout=60.0, auth=_manager_auth()) as client:
            tarball_path = await download_and_verify(
                client=client,
                pack_id=pack.id,
                release=pack.release,
                expected_sha256=pack.tarball_sha256,
                dest_dir=tarball_dir,
            )
        adapter = await load_adapter(
            pack_id=pack.id,
            release=pack.release,
            tarball_path=tarball_path,
            runtime_dir=runtime_dir,
        )
        adapter_registry.set(pack.id, pack.release, adapter)

    return _load


async def _stop_grid_node_supervisors_for_shutdown(
    manager: AppiumProcessManager,
    *,
    timeout_sec: float = GRID_NODE_SHUTDOWN_TIMEOUT_SEC,
) -> None:
    supervisors = list(manager._grid_supervisors.items())
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
        # Leave timed-out supervisor handles in `manager._grid_supervisors`
        # so the immediate `appium_mgr.shutdown()` can re-attempt the stop.
        # `GridNodeSupervisorHandle.stop()` is idempotent (cancelled task is a
        # no-op on the second pass), so retrying is safe and orphans nothing.
        logger.warning("timed out stopping grid node supervisors during shutdown")
        return
    for result in results:
        if isinstance(result, int):
            manager._grid_supervisors.pop(result, None)
        elif isinstance(result, Exception):
            logger.warning("failed to stop grid node supervisor during shutdown", exc_info=result)


def _get_network_devices() -> list[dict[str, Any]]:
    """Return network devices from currently running Appium processes."""
    devices: list[dict[str, Any]] = []
    for info in appium_mgr.list_running():
        if re.match(r"\d+\.\d+\.\d+\.\d+:\d+", info.connection_target):
            ip, _, port_str = info.connection_target.rpartition(":")
            devices.append({"connection_target": info.connection_target, "ip_address": ip, "port": int(port_str)})
    return devices


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:

    await refresh_capabilities_snapshot()
    capabilities_task = asyncio.create_task(capabilities_refresh_loop(refresh_immediately=False))

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

    env_host_id = os.environ.get("AGENT_HOST_ID")
    backend_url = os.environ.get("AGENT_BACKEND_URL") or agent_settings.manager_url
    if env_host_id:
        host_identity.set(env_host_id)
        app.state.pack_state_loop_enabled = True

    reg_task = asyncio.create_task(
        registration_loop(
            agent_settings.manager_url,
            agent_settings.agent_port,
            host_identity,
        )
    )
    appium_mgr.set_runtime_registry(runtime_registry)
    appium_mgr.set_adapter_registry(adapter_registry)

    pack_task: asyncio.Task[None] | None = None
    if backend_url:
        pack_task = asyncio.create_task(
            _start_pack_loop_when_ready(
                app, host_identity, backend_url, runtime_registry, adapter_registry, sidecar_supervisor
            )
        )

    try:
        yield
    finally:
        if pack_task is not None:
            pack_task.cancel()
        reg_task.cancel()
        capabilities_task.cancel()
        await _stop_grid_node_supervisors_for_shutdown(appium_mgr)
        await appium_mgr.shutdown()
        await sidecar_supervisor.shutdown()
        await close_shared_http_client()


app = FastAPI(title="GridFleet Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)  # inner: enforces Basic auth on /agent/*
app.add_middleware(RequestContextMiddleware)  # outer: binds request_id, runs first

app.include_router(host_router)
app.include_router(appium_router)
app.include_router(pack_router)
app.include_router(plugins_router)
app.include_router(tools_router)
app.include_router(terminal_router)


class GridNodeReregisterRequest(BaseModel):
    target_run_id: UUID | None = None


class GridNodeReregisterResponse(BaseModel):
    grid_run_id: UUID | None


def _grid_node_service_for(node_id: str) -> GridNodeServiceProtocol:
    for supervisor in appium_mgr._grid_supervisors.values():
        service = supervisor.service
        if service is not None and service.node_id == node_id:
            return service
    raise http_exc(
        status_code=404,
        code=AgentErrorCode.DEVICE_NOT_FOUND,
        message=f"No running grid node is registered for node_id={node_id}",
    )


@app.post("/grid/node/{node_id}/reregister", response_model=GridNodeReregisterResponse)
async def reregister_grid_node(node_id: str, payload: GridNodeReregisterRequest) -> GridNodeReregisterResponse:
    service = _grid_node_service_for(node_id)
    caps = service.slot_stereotype_caps()
    caps["gridfleet:run_id"] = str(payload.target_run_id) if payload.target_run_id is not None else "free"
    await service.reregister_with_stereotype(new_caps=caps)
    return GridNodeReregisterResponse(grid_run_id=payload.target_run_id)
