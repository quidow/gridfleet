"""HTTP routes for ``/agent/appium/*``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from agent_app.appium import appium_mgr
from agent_app.appium.process import (
    AlreadyRunningError,
    DeviceNotFoundError,
    InvalidStartPayloadError,
    PortOccupiedError,
    RuntimeMissingError,
    RuntimeNotInstalledError,
    StartupTimeoutError,
)
from agent_app.appium.schemas import (  # noqa: TC001 - FastAPI resolves these at runtime
    AppiumReconfigureRequest,
    AppiumStartRequest,
    AppiumStopRequest,
)
from agent_app.error_codes import AgentErrorCode, http_exc

router = APIRouter(prefix="/agent/appium", tags=["appium"])


@router.post("/start")
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


@router.post("/{port}/reconfigure")
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


@router.post("/stop")
async def stop_appium(req: AppiumStopRequest) -> dict[str, Any]:
    await appium_mgr.stop(req.port)
    return {"stopped": True, "port": req.port}


@router.get("/{port}/status")
async def appium_status(port: int) -> dict[str, Any]:
    return await appium_mgr.status(port)


@router.get("/{port}/logs")
async def appium_logs(port: int, lines: int = 100) -> dict[str, Any]:
    log_lines = appium_mgr.get_logs(port, lines=min(lines, 5000))
    return {"port": port, "lines": log_lines, "count": len(log_lines)}
