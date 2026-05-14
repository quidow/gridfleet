import asyncio
import logging
import os
import platform
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field

from agent_app import __version__
from agent_app.api_auth import BasicAuthMiddleware
from agent_app.appium.process import (
    AlreadyRunningError,
    AppiumProcessManager,
    DeviceNotFoundError,
    InvalidStartPayloadError,
    PortOccupiedError,
    RuntimeMissingError,
    RuntimeNotInstalledError,
    StartupTimeoutError,
)
from agent_app.appium.schemas import AppiumReconfigureRequest
from agent_app.capabilities import (
    capabilities_refresh_loop,
    get_capabilities_snapshot,
    refresh_capabilities_snapshot,
)
from agent_app.config import agent_settings
from agent_app.error_codes import AgentErrorCode, http_exc
from agent_app.grid_node.supervisor import GridNodeServiceProtocol, GridNodeSupervisorHandle
from agent_app.host_telemetry import get_host_telemetry
from agent_app.http_client import close as close_shared_http_client
from agent_app.http_client import get_client as get_shared_http_client
from agent_app.observability import RequestContextMiddleware, configure_logging
from agent_app.pack.adapter_dispatch import dispatch_feature_action
from agent_app.pack.adapter_loader import load_adapter
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.discovery import enumerate_pack_candidates, pack_device_properties
from agent_app.pack.dispatch import (
    adapter_health_check,
    adapter_lifecycle_action,
    adapter_normalize_device,
    adapter_telemetry,
)
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.manifest import DesiredPack, resolve_desired_platform
from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.sidecar_supervisor import SidecarSupervisor
from agent_app.pack.state import AdapterLoaderFn, PackStateClient, PackStateLoop
from agent_app.pack.tarball_fetch import download_and_verify
from agent_app.pack.version_catalog import NpmVersionCatalog
from agent_app.plugin_manager import get_installed_plugins, sync_plugins
from agent_app.registration import registration_loop
from agent_app.terminal_ws import handle_terminal
from agent_app.tools_manager import get_tool_status
from agent_app.version_guidance import get_version_guidance

configure_logging()
logger = logging.getLogger(__name__)

appium_mgr = AppiumProcessManager()
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


class AppiumStartRequest(BaseModel):
    connection_target: str
    port: int
    grid_url: str
    plugins: list[str] | None = None
    extra_caps: dict[str, Any] | None = None
    stereotype_caps: dict[str, Any] | None = None
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: UUID | None = None
    allocated_caps: dict[str, Any] | None = None
    device_type: str | None = None
    ip_address: str | None = None
    session_override: bool = True
    headless: bool = True
    pack_id: str
    platform_id: str
    appium_platform_name: str | None = None
    workaround_env: dict[str, str] | None = None
    insecure_features: list[str] = []
    grid_slots: list[str] = ["native"]
    lifecycle_actions: list[dict[str, Any]] = []
    connection_behavior: dict[str, Any] = {}


class AppiumStopRequest(BaseModel):
    port: int


class PluginConfig(BaseModel):
    name: str
    version: str
    source: str
    package: str | None = None


class PluginSyncRequest(BaseModel):
    plugins: list[PluginConfig]


class GridNodeReregisterRequest(BaseModel):
    target_run_id: UUID | None = None


class GridNodeReregisterResponse(BaseModel):
    grid_run_id: UUID | None


class FeatureActionRequest(BaseModel):
    pack_id: str
    args: dict[str, Any] = {}
    device_identity_value: str | None = None


class NormalizeDeviceRequest(BaseModel):
    pack_id: str
    pack_release: str
    platform_id: str
    raw_input: dict[str, Any]


class NormalizeDeviceResponse(BaseModel):
    identity_scheme: str
    identity_scope: str
    identity_value: str
    connection_target: str
    ip_address: str
    device_type: str
    connection_type: str
    os_version: str
    manufacturer: str = ""
    model: str = ""
    model_number: str = ""
    software_versions: dict[str, str] = Field(default_factory=dict)
    field_errors: list[dict[str, str]]


class _FeatureActionContext:
    """Concrete LifecycleContext used when dispatching feature actions."""

    __slots__ = ("device_identity_value", "host_id")

    def __init__(self, host_id: str, device_identity_value: str) -> None:
        self.host_id = host_id
        self.device_identity_value = device_identity_value


def _latest_desired(request: Request) -> list[Any]:
    loop = getattr(request.app.state, "pack_state_loop", None)
    return list(loop.latest_desired_packs or []) if loop else []


def _release_for_pack(request: Request, pack_id: str) -> str | None:
    for pack in _latest_desired(request):
        if getattr(pack, "id", None) == pack_id:
            return str(getattr(pack, "release", ""))
    return None


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


@app.get("/agent/health")
async def health() -> dict[str, Any]:
    capabilities = get_capabilities_snapshot()
    payload: dict[str, Any] = {
        "status": "ok",
        "hostname": platform.node(),
        "os_type": platform.system().lower(),
        "version": __version__,
        "missing_prerequisites": capabilities.get("missing_prerequisites", []),
        "capabilities": capabilities,
    }
    payload["appium_processes"] = appium_mgr.process_snapshot()
    payload["version_guidance"] = get_version_guidance().to_payload()
    return payload


@app.post("/grid/node/{node_id}/reregister", response_model=GridNodeReregisterResponse)
async def reregister_grid_node(node_id: str, payload: GridNodeReregisterRequest) -> GridNodeReregisterResponse:
    service = _grid_node_service_for(node_id)
    caps = service.slot_stereotype_caps()
    caps["gridfleet:run_id"] = str(payload.target_run_id) if payload.target_run_id is not None else "free"
    await service.reregister_with_stereotype(new_caps=caps)
    return GridNodeReregisterResponse(grid_run_id=payload.target_run_id)


@app.get("/agent/host/telemetry")
async def host_telemetry() -> dict[str, Any]:
    return await get_host_telemetry()


@app.get("/agent/pack/devices")
async def pack_devices(
    request: Request,
) -> dict[str, Any]:
    loop = getattr(request.app.state, "pack_state_loop", None)
    desired = loop.latest_desired_packs if loop else None
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    host_identity = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else None
    return await enumerate_pack_candidates(
        desired,
        adapter_registry=adapter_registry,
        host_id=host_id_value or "",
    )


@app.get("/agent/pack/devices/{connection_target}/properties")
async def pack_device_properties_route(
    request: Request,
    connection_target: str,
    pack_id: str = Query(...),
) -> dict[str, Any]:
    loop = getattr(request.app.state, "pack_state_loop", None)
    desired = loop.latest_desired_packs if loop else None
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    host_identity = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else None
    data = await pack_device_properties(
        connection_target,
        pack_id,
        desired,
        adapter_registry=adapter_registry,
        host_id=host_id_value or "",
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"Pack device {connection_target} not found")
    return data


@app.get("/agent/pack/devices/{connection_target}/health")
async def pack_device_health_route(
    request: Request,
    connection_target: str,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    device_type: str = Query(...),
    connection_type: str | None = Query(None),
    ip_address: str | None = Query(None),
    allow_boot: bool = Query(False),
    headless: bool = Query(True),
    ip_ping_timeout_sec: float | None = Query(None),
    ip_ping_count: int | None = Query(None),
) -> dict[str, Any]:
    """Pack-shaped device health check dispatched through the loaded adapter."""
    platform_def = resolve_desired_platform(_latest_desired(request), pack_id=pack_id, platform_id=platform_id)
    if platform_def is None:
        raise HTTPException(status_code=404, detail=f"Unknown desired pack platform {pack_id}:{platform_id}")
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    release = _release_for_pack(request, pack_id)
    if adapter_registry is not None and release is not None:
        payload = await adapter_health_check(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            identity_value=connection_target,
            allow_boot=allow_boot,
            platform_id=platform_id,
            device_type=device_type,
            connection_type=connection_type,
            ip_address=ip_address,
            ip_ping_timeout_sec=ip_ping_timeout_sec,
            ip_ping_count=ip_ping_count,
        )
        if payload is not None:
            return payload
    return {
        "healthy": None,
        "checks": [
            {
                "check_id": "adapter_unavailable",
                "ok": False,
                "message": f"Adapter not loaded for pack {pack_id}:{platform_id}",
            }
        ],
    }


@app.get("/agent/pack/devices/{connection_target}/telemetry")
async def pack_device_telemetry_route(
    request: Request,
    connection_target: str,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    device_type: str = Query(...),
    connection_type: str | None = Query(None),
    ip_address: str | None = Query(None),
) -> dict[str, Any]:
    """Pack-shaped device telemetry dispatched through the loaded adapter."""
    platform_def = resolve_desired_platform(_latest_desired(request), pack_id=pack_id, platform_id=platform_id)
    if platform_def is None:
        raise HTTPException(status_code=404, detail=f"Unknown desired pack platform {pack_id}:{platform_id}")
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    release = _release_for_pack(request, pack_id)
    telemetry = (
        await adapter_telemetry(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            identity_value=connection_target,
            connection_target=connection_target,
        )
        if adapter_registry is not None and release is not None
        else None
    )
    if telemetry is None:
        raise HTTPException(status_code=404, detail=f"Device {connection_target} not found or not connected")
    return telemetry


@app.post("/agent/pack/devices/{connection_target}/lifecycle/{action}")
async def pack_device_lifecycle_route(
    request: Request,
    connection_target: str,
    action: str,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    args: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Pack-shaped device lifecycle action dispatched through the loaded adapter."""
    platform_def = resolve_desired_platform(_latest_desired(request), pack_id=pack_id, platform_id=platform_id)
    if platform_def is None:
        raise HTTPException(status_code=404, detail=f"Unknown desired pack platform {pack_id}:{platform_id}")
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    host_identity = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else ""
    release = _release_for_pack(request, pack_id)
    if adapter_registry is not None and release is not None:
        payload = await adapter_lifecycle_action(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            host_id=host_id_value or "",
            identity_value=connection_target,
            action=action,
            args=args,
        )
        if payload is not None:
            return payload
    return {
        "success": False,
        "detail": f"Adapter not loaded for pack {pack_id}:{platform_id}",
    }


@app.post("/agent/pack/features/{feature_id}/actions/{action_id}")
async def feature_action_route(
    request: Request,
    feature_id: str,
    action_id: str,
    body: FeatureActionRequest,
) -> dict[str, Any]:
    """Dispatch a feature action to the pack's adapter.

    Body: ``{pack_id: str, args: dict, device_identity_value: str | None}``.
    Returns ``FeatureActionResult`` serialised as JSON.
    Responds 404 when no adapter is loaded for ``pack_id``.
    """
    adapter_registry: AdapterRegistry | None = getattr(request.app.state, "adapter_registry", None)
    adapter = adapter_registry.get_current(body.pack_id) if adapter_registry is not None else None
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {body.pack_id!r}")

    host_identity: HostIdentity | None = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else ""

    ctx = _FeatureActionContext(
        host_id=host_id_value or "",
        device_identity_value=body.device_identity_value or "",
    )
    result = await dispatch_feature_action(adapter, feature_id, action_id, body.args, ctx)
    return {"ok": result.ok, "detail": result.detail, "data": result.data}


@app.post("/agent/pack/devices/normalize", response_model=NormalizeDeviceResponse)
async def normalize_device_route(request: Request, req: NormalizeDeviceRequest) -> dict[str, Any]:
    adapter_registry: AdapterRegistry | None = getattr(request.app.state, "adapter_registry", None)
    host_identity: HostIdentity | None = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else ""
    if adapter_registry is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {req.pack_id!r}")

    result = await adapter_normalize_device(
        adapter_registry=adapter_registry,
        pack_id=req.pack_id,
        pack_release=req.pack_release,
        host_id=host_id_value or "",
        platform_id=req.platform_id,
        raw_input=req.raw_input,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {req.pack_id!r}")
    return result


@app.post("/agent/appium/start")
async def start_appium(req: AppiumStartRequest) -> dict[str, Any]:
    try:
        info = await appium_mgr.start(
            connection_target=req.connection_target,
            platform_id=req.platform_id,
            port=req.port,
            grid_url=req.grid_url,
            plugins=req.plugins,
            extra_caps=req.extra_caps,
            stereotype_caps=req.stereotype_caps,
            accepting_new_sessions=req.accepting_new_sessions,
            stop_pending=req.stop_pending,
            grid_run_id=req.grid_run_id,
            session_override=req.session_override,
            device_type=req.device_type,
            ip_address=req.ip_address,
            headless=req.headless,
            pack_id=req.pack_id,
            appium_platform_name=req.appium_platform_name,
            workaround_env=req.workaround_env,
            insecure_features=req.insecure_features,
            grid_slots=req.grid_slots,
            lifecycle_actions=req.lifecycle_actions,
            connection_behavior=req.connection_behavior,
        )
    except PortOccupiedError as e:
        raise http_exc(status_code=409, code=AgentErrorCode.PORT_OCCUPIED, message=str(e)) from e
    except AlreadyRunningError as e:
        raise http_exc(status_code=409, code=AgentErrorCode.ALREADY_RUNNING, message=str(e)) from e
    except StartupTimeoutError as e:
        raise http_exc(status_code=504, code=AgentErrorCode.STARTUP_TIMEOUT, message=str(e)) from e
    except (RuntimeMissingError, RuntimeNotInstalledError) as e:
        raise http_exc(status_code=503, code=AgentErrorCode.RUNTIME_MISSING, message=str(e)) from e
    except DeviceNotFoundError as e:
        raise http_exc(status_code=404, code=AgentErrorCode.DEVICE_NOT_FOUND, message=str(e)) from e
    except InvalidStartPayloadError as e:
        raise http_exc(status_code=400, code=AgentErrorCode.INVALID_PAYLOAD, message=str(e)) from e
    except RuntimeError as e:
        raise http_exc(status_code=500, code=AgentErrorCode.INTERNAL_ERROR, message=str(e)) from e
    except Exception as e:
        raise http_exc(status_code=500, code=AgentErrorCode.INTERNAL_ERROR, message=str(e)) from e
    return {"pid": info.pid, "port": info.port, "connection_target": info.connection_target}


@app.post("/agent/appium/{port}/reconfigure")
async def reconfigure_appium(port: int, req: AppiumReconfigureRequest) -> dict[str, Any]:
    try:
        await appium_mgr.reconfigure(
            port,
            accepting_new_sessions=req.accepting_new_sessions,
            stop_pending=req.stop_pending,
            grid_run_id=req.grid_run_id,
        )
    except DeviceNotFoundError as exc:
        raise http_exc(status_code=404, code=AgentErrorCode.DEVICE_NOT_FOUND, message=str(exc)) from exc
    return {
        "port": port,
        "accepting_new_sessions": req.accepting_new_sessions,
        "stop_pending": req.stop_pending,
        "grid_run_id": str(req.grid_run_id) if req.grid_run_id else None,
    }


@app.post("/agent/appium/stop")
async def stop_appium(req: AppiumStopRequest) -> dict[str, Any]:
    await appium_mgr.stop(req.port)
    return {"stopped": True, "port": req.port}


@app.get("/agent/appium/{port}/status")
async def appium_status(port: int) -> dict[str, Any]:
    return await appium_mgr.status(port)


@app.get("/agent/appium/{port}/logs")
async def appium_logs(port: int, lines: int = 100) -> dict[str, Any]:
    log_lines = appium_mgr.get_logs(port, lines=min(lines, 5000))
    return {"port": port, "lines": log_lines, "count": len(log_lines)}


@app.get("/agent/plugins")
async def list_plugins() -> list[dict[str, str]]:
    return await get_installed_plugins()


@app.post("/agent/plugins/sync")
async def sync_agent_plugins(req: PluginSyncRequest) -> dict[str, Any]:
    configs = [plugin.model_dump() for plugin in req.plugins]
    return await sync_plugins(configs)


@app.get("/agent/tools/status")
async def agent_tools_status() -> dict[str, Any]:
    return await get_tool_status()


@app.websocket("/agent/terminal")
async def agent_terminal(ws: WebSocket) -> None:
    await handle_terminal(ws)
