"""HTTP routes for ``/agent/appium/*``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, status

from agent_app.appium import appium_mgr
from agent_app.appium.exceptions import (
    AlreadyRunningError,
    DeviceNotFoundError,
    InvalidStartPayloadError,
    PortOccupiedError,
    RuntimeMissingError,
    RuntimeNotInstalledError,
    StartupTimeoutError,
)
from agent_app.appium.schemas import (
    AppiumLogsResponse,
    AppiumReconfigureRequest,
    AppiumReconfigureResponse,
    AppiumStartRequest,
    AppiumStartResponse,
    AppiumStatusResponse,
    AppiumStopRequest,
    AppiumStopResponse,
)
from agent_app.error_codes import AgentErrorCode, ErrorEnvelope, http_exc

router = APIRouter(prefix="/agent/appium", tags=["appium"])


@router.post(
    "/start",
    response_model=AppiumStartResponse,
    status_code=status.HTTP_200_OK,
    summary="Start a managed Appium process for a device",
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorEnvelope, "description": "INVALID_PAYLOAD"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "DEVICE_NOT_FOUND"},
        status.HTTP_409_CONFLICT: {"model": ErrorEnvelope, "description": "PORT_OCCUPIED or ALREADY_RUNNING"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorEnvelope, "description": "INTERNAL_ERROR"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorEnvelope, "description": "RUNTIME_MISSING"},
        status.HTTP_504_GATEWAY_TIMEOUT: {"model": ErrorEnvelope, "description": "STARTUP_TIMEOUT"},
    },
)
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
    return {"pid": info.pid, "port": info.port, "connection_target": info.connection_target}


@router.post(
    "/{port}/reconfigure",
    response_model=AppiumReconfigureResponse,
    status_code=status.HTTP_200_OK,
    summary="Toggle accepting-sessions / drain-pending / run scope",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "DEVICE_NOT_FOUND"},
    },
)
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
        "grid_run_id": req.grid_run_id,
    }


@router.post(
    "/stop",
    response_model=AppiumStopResponse,
    status_code=status.HTTP_200_OK,
    summary="Stop a managed Appium process by port",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "DEVICE_NOT_FOUND"},
    },
)
async def stop_appium(req: AppiumStopRequest) -> dict[str, Any]:
    await appium_mgr.stop(req.port)
    return {"stopped": True, "port": req.port}


@router.get(
    "/{port}/status",
    response_model=AppiumStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Process info for a managed Appium port",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "DEVICE_NOT_FOUND"},
    },
)
async def appium_status(port: int) -> dict[str, Any]:
    return await appium_mgr.status(port)


@router.get(
    "/{port}/logs",
    response_model=AppiumLogsResponse,
    status_code=status.HTTP_200_OK,
    summary="Recent stdout/stderr lines for a managed Appium",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "DEVICE_NOT_FOUND"},
    },
)
async def appium_logs(port: int, lines: int = Query(100, ge=1, le=5000)) -> dict[str, Any]:
    log_lines = appium_mgr.get_logs(port, lines=lines)
    return {"port": port, "lines": log_lines, "count": len(log_lines)}
