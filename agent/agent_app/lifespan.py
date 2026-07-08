"""FastAPI lifespan and its supporting helpers.

Extracted from ``agent_app/main.py``. Owns the long-running background
tasks (capabilities refresh, registration, pack state loop) plus the
adapter loader used by the pack state loop, plus the pack-state HTTP client.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_app.appium import appium_mgr
from agent_app.appium.node_state import NodeStateClient, NodeStateLoop
from agent_app.config import agent_settings
from agent_app.host.capabilities import CapabilitiesCache
from agent_app.host.version_guidance import VersionGuidanceStore
from agent_app.http_client import close as close_shared_http_client
from agent_app.http_client import get_client as get_shared_http_client
from agent_app.pack.adapter_loader import load_adapter
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.runtime import AppiumRuntimeManager
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.sidecar_supervisor import SidecarSupervisor
from agent_app.pack.state import PackStateClient, PackStateLoop
from agent_app.pack.tarball_fetch import download_and_verify
from agent_app.pack.version_catalog import NpmVersionCatalog
from agent_app.registration import RegistrationService
from agent_app.registration import manager_auth as _manager_auth  # tests patch agent_app.lifespan._manager_auth

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from fastapi import FastAPI

    from agent_app.pack.manifest import DesiredPack
    from agent_app.pack.runtime import RuntimeEnv
    from agent_app.pack.state import AdapterLoaderFn

logger = logging.getLogger(__name__)


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


class HttpPackStateClient(PackStateClient):
    def __init__(self, base_url: str, host_identity: HostIdentity) -> None:
        self._base = base_url.rstrip("/")
        # Hold the identity reference (not just the current value) so that a
        # manager-issued host_id rotation during a long-lived pack loop is
        # picked up on the next request instead of leaving us pinned to the
        # stale id captured at construction.
        self._host_identity = host_identity

    def _current_host_id(self) -> str:
        host_id = self._host_identity.get()
        if host_id is None:
            raise RuntimeError("HttpPackStateClient used before host identity was assigned")
        return host_id

    async def fetch_desired(self) -> dict[str, Any]:
        client = get_shared_http_client()
        kwargs: dict[str, Any] = {
            "params": {"host_id": self._current_host_id()},
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


class HttpNodeStateClient(NodeStateClient):
    def __init__(self, base_url: str, host_identity: HostIdentity) -> None:
        self._base = base_url.rstrip("/")
        self._host_identity = host_identity

    async def fetch_desired(self) -> dict[str, Any]:
        host_id = self._host_identity.get()
        if host_id is None:
            raise RuntimeError("HttpNodeStateClient used before host identity was assigned")
        kwargs: dict[str, Any] = {"params": {"host_id": host_id}, "timeout": 15.0}
        if (auth := _manager_auth()) is not None:
            kwargs["auth"] = auth
        resp = await get_shared_http_client().get(f"{self._base}/agent/appium-nodes/desired", **kwargs)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


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
    await host_identity.wait()
    app.state.pack_state_loop_enabled = True
    client = HttpPackStateClient(backend_url, host_identity)
    runtime_mgr = AppiumRuntimeManager()
    adapter_loader = _build_adapter_loader(backend_url, adapter_registry)
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_identity=host_identity,
        runtime_registry=runtime_registry,
        adapter_registry=adapter_registry,
        adapter_loader=adapter_loader,
        sidecar_supervisor=sidecar_supervisor,
        version_catalog=NpmVersionCatalog(),
    )
    app.state.pack_state_loop = loop
    await loop.run_forever()


async def _start_node_loop_when_ready(app: FastAPI, host_identity: HostIdentity, backend_url: str) -> None:
    await host_identity.wait()
    loop = NodeStateLoop(
        client=HttpNodeStateClient(backend_url, host_identity),
        manager=appium_mgr,
        poll_interval=agent_settings.runtime.node_poll_interval_sec,
    )
    app.state.node_state_loop = loop
    await loop.run_forever()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    host_identity = HostIdentity()
    runtime_registry = RuntimeRegistry()
    adapter_registry = AdapterRegistry()
    capabilities_cache = CapabilitiesCache(adapter_registry=adapter_registry)
    app.state.capabilities_cache = capabilities_cache
    await capabilities_cache.refresh()
    capabilities_task = asyncio.create_task(capabilities_cache.run_refresh_loop(refresh_immediately=False))
    capabilities_task.add_done_callback(_watchdog("capabilities_refresh"))
    sidecar_supervisor = SidecarSupervisor()
    boot_id = uuid4()
    app.state.host_identity = host_identity
    app.state.runtime_registry = runtime_registry
    app.state.adapter_registry = adapter_registry
    app.state.sidecar_supervisor = sidecar_supervisor
    app.state.boot_id = boot_id
    app.state.pack_state_loop_enabled = False
    app.state.pack_state_loop = None
    app.state.node_state_loop = None
    version_guidance = VersionGuidanceStore()
    app.state.version_guidance = version_guidance

    env_host_id = agent_settings.core.host_id
    backend_url = agent_settings.manager.effective_backend_url
    if env_host_id:
        host_identity.set(env_host_id)
        app.state.pack_state_loop_enabled = True

    registration = RegistrationService(
        capabilities_cache=capabilities_cache,
        version_guidance=version_guidance,
        host_identity=host_identity,
    )

    reg_task: asyncio.Task[None]

    def _start_registration_task() -> asyncio.Task[None]:
        nonlocal reg_task
        reg_task = asyncio.create_task(
            registration.run(
                agent_settings.manager.manager_url,
                agent_settings.core.agent_port,
            )
        )
        reg_task.add_done_callback(_watchdog("registration", _start_registration_task))
        return reg_task

    reg_task = _start_registration_task()
    appium_mgr.set_runtime_registry(runtime_registry)
    appium_mgr.set_adapter_registry(adapter_registry)
    appium_mgr.start_log_maintenance()

    pack_task: asyncio.Task[None] | None = None
    if backend_url:
        pack_task = asyncio.create_task(
            _start_pack_loop_when_ready(
                app, host_identity, backend_url, runtime_registry, adapter_registry, sidecar_supervisor
            )
        )
        pack_task.add_done_callback(_watchdog("pack_state_loop"))

    node_task: asyncio.Task[None] | None = None
    if backend_url and agent_settings.runtime.node_pull_enabled:
        node_task = asyncio.create_task(_start_node_loop_when_ready(app, host_identity, backend_url))
        node_task.add_done_callback(_watchdog("node_state_loop"))

    try:
        yield
    finally:
        if node_task is not None:
            node_task.cancel()
        if pack_task is not None:
            pack_task.cancel()
        reg_task.cancel()
        capabilities_task.cancel()
        await appium_mgr.shutdown()
        await sidecar_supervisor.shutdown()
        await close_shared_http_client()
