import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent_comm.probe_result import ProbeResult
from app.core.database import async_session
from app.core.leader import state_store as control_plane_state_store
from app.core.observability import get_logger, observe_background_loop
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import capability as capability_service
from app.devices.services import health as device_health
from app.devices.services import readiness as device_readiness
from app.devices.services import state as device_state
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.viability_types import SessionViabilityCheckedBy
from app.settings import settings_service

__all__ = [
    "PROBE_TEST_NAME",
    "SESSION_VIABILITY_KEY",
    "SESSION_VIABILITY_RUNNING_NAMESPACE",
    "SESSION_VIABILITY_STATE_NAMESPACE",
    "_check_due_devices",
    "_extract_session_error",
    "_format_http_error",
    "_get_grid_probe_client",
    "_parse_timestamp",
    "_should_run_scheduled_probe",
    "build_probe_capabilities",
    "close",
    "configure_health_failure_handler",
    "get_session_viability",
    "get_session_viability_control_plane_state",
    "grid_probe_response_to_result",
    "probe_session_via_grid",
    "record_session_viability_result",
    "reset_session_viability_control_plane_state",
    "run_session_viability_probe",
    "session_viability_loop",
    "set_session_viability_control_plane_entry",
]

SESSION_VIABILITY_KEY = "session_viability"
SESSION_VIABILITY_STATE_NAMESPACE = "session_viability.state"
SESSION_VIABILITY_RUNNING_NAMESPACE = "session_viability.running"
logger = get_logger(__name__)
LOOP_NAME = "session_viability"
is_ready_for_use_async = device_readiness.is_ready_for_use_async
readiness_error_detail_async = device_readiness.readiness_error_detail_async
ready_operational_state = device_state.ready_operational_state
set_operational_state = device_state.set_operational_state

# Shared httpx.AsyncClient for grid probe calls. Per-call instantiation leaks
# ~0.8 MB on macOS (TLS contexts not released by the native allocator).
_grid_probe_client: httpx.AsyncClient | None = None


def _get_grid_probe_client() -> httpx.AsyncClient:
    global _grid_probe_client
    if _grid_probe_client is None or _grid_probe_client.is_closed:
        _grid_probe_client = httpx.AsyncClient()
    return _grid_probe_client


async def close() -> None:
    """Close the shared probe client. Call from app shutdown."""
    global _grid_probe_client
    if _grid_probe_client is not None and not _grid_probe_client.is_closed:
        await _grid_probe_client.aclose()
    _grid_probe_client = None


class HealthFailureHandler(Protocol):
    async def __call__(
        self,
        db: AsyncSession,
        device: Device,
        *,
        source: str,
        reason: str,
    ) -> object:
        raise NotImplementedError


_health_failure_handler: HealthFailureHandler | None = None


def configure_health_failure_handler(handler: HealthFailureHandler | None) -> None:
    global _health_failure_handler
    _health_failure_handler = handler


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def get_session_viability(db: AsyncSession, device: Device) -> dict[str, Any] | None:
    state = await control_plane_state_store.get_value(db, SESSION_VIABILITY_STATE_NAMESPACE, str(device.id))
    if state is None:
        return None
    return {
        "status": state.get("status"),
        "last_attempted_at": state.get("last_attempted_at"),
        "last_succeeded_at": state.get("last_succeeded_at"),
        "error": state.get("error"),
        "checked_by": state.get("checked_by"),
    }


async def _write_session_viability(
    db: AsyncSession,
    device: Device,
    *,
    status: str,
    attempted_at: str,
    error: str | None,
    checked_by: SessionViabilityCheckedBy,
) -> dict[str, Any]:
    previous = await get_session_viability(db, device) or {}
    state = {
        "status": status,
        "last_attempted_at": attempted_at,
        "last_succeeded_at": attempted_at if status == "passed" else previous.get("last_succeeded_at"),
        "error": error,
        "checked_by": checked_by,
    }
    await control_plane_state_store.set_value(db, SESSION_VIABILITY_STATE_NAMESPACE, str(device.id), state)
    await device_health.update_session_viability(db, device, status=status, error=error)
    return state


async def record_session_viability_result(
    db: AsyncSession,
    device: Device,
    *,
    status: str,
    error: str | None = None,
    checked_by: SessionViabilityCheckedBy,
) -> dict[str, Any]:
    config_changed = _clear_session_viability_from_config(device)
    state = await _write_session_viability(
        db,
        device,
        status=status,
        attempted_at=_now_iso(),
        error=error,
        checked_by=checked_by,
    )
    if config_changed:
        await db.flush()
    return state


def _clear_session_viability_from_config(device: Device) -> bool:
    config = device.device_config or {}
    if SESSION_VIABILITY_KEY not in config:
        return False
    next_config = dict(config)
    next_config.pop(SESSION_VIABILITY_KEY, None)
    device.device_config = next_config
    return True


async def _is_probe_running(db: AsyncSession, device_key: str) -> bool:
    return await control_plane_state_store.get_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key) is not None


async def _should_run_scheduled_probe(db: AsyncSession, device: Device, interval_sec: int) -> bool:
    if interval_sec <= 0:
        return False
    if device.operational_state != DeviceOperationalState.available or device.hold is not None:
        return False
    if not await is_ready_for_use_async(db, device):
        return False
    if await _is_probe_running(db, str(device.id)):
        return False

    previous = await get_session_viability(db, device)
    if previous is None:
        return True

    last_attempted_at = _parse_timestamp(previous.get("last_attempted_at"))
    if last_attempted_at is None:
        return True

    elapsed = (datetime.now(UTC) - last_attempted_at).total_seconds()
    return elapsed >= interval_sec


def _build_session_payload(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "capabilities": {
            "alwaysMatch": capabilities,
            "firstMatch": [{}],
        }
    }


def _extract_session_error(data: object) -> str:
    if isinstance(data, dict):
        value = data.get("value")
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
            error = value.get("error")
            if isinstance(error, str) and error:
                return error
        message = data.get("message")
        if isinstance(message, str) and message:
            return message
    return "Session probe failed"


def _format_http_error(exc: httpx.HTTPError) -> str:
    message = str(exc).strip()
    if message:
        return message
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) is not None:
        return f"{exc.__class__.__name__} (HTTP {response.status_code})"
    request = getattr(exc, "request", None)
    if request is not None and getattr(request, "url", None) is not None:
        return f"{exc.__class__.__name__} while calling {request.url}"
    return exc.__class__.__name__


def build_probe_capabilities(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        **capabilities,
        "gridfleet:probeSession": True,
        "gridfleet:testName": PROBE_TEST_NAME,
    }


def grid_probe_response_to_result(result: tuple[bool, str | None]) -> ProbeResult:
    ok, detail = result
    if ok:
        return ProbeResult(status="ack")
    if detail is None:
        return ProbeResult(status="refused")
    infrastructure_markers = (
        "Session create request failed:",
        "Session created but cleanup failed:",
        "Session created but cleanup failed (",
    )
    if detail.startswith(infrastructure_markers):
        return ProbeResult(status="indeterminate", detail=detail)
    return ProbeResult(status="refused", detail=detail)


async def probe_session_via_grid(
    capabilities: dict[str, Any],
    timeout_sec: int,
    *,
    grid_url: str | None = None,
) -> tuple[bool, str | None]:
    base = (grid_url or settings_service.get("grid.hub_url")).rstrip("/")
    client = _get_grid_probe_client()
    try:
        create_resp = await client.post(
            f"{base}/session", json=_build_session_payload(capabilities), timeout=timeout_sec
        )
    except httpx.HTTPError as exc:
        return False, f"Session create request failed: {_format_http_error(exc)}"

    if create_resp.status_code >= 400:
        try:
            return False, _extract_session_error(create_resp.json())
        except ValueError:
            return False, create_resp.text or "Session create failed"

    try:
        data = create_resp.json()
    except ValueError:
        return False, "Session create returned invalid JSON"

    session_id = data.get("sessionId")
    if not session_id and isinstance(data.get("value"), dict):
        session_id = data["value"].get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return False, "Session create did not return a session id"

    try:
        delete_resp = await client.delete(f"{base}/session/{session_id}", timeout=timeout_sec)
        if delete_resp.status_code >= 400:
            return False, f"Session created but cleanup failed ({delete_resp.status_code})"
    except httpx.HTTPError as exc:
        return False, f"Session created but cleanup failed: {_format_http_error(exc)}"

    return True, None


async def run_session_viability_probe(
    db: AsyncSession,
    device: Device,
    *,
    checked_by: SessionViabilityCheckedBy,
) -> dict[str, Any]:
    device_key = str(device.id)
    previous_state: DeviceOperationalState | None = None
    acquired = await control_plane_state_store.try_claim_value(
        db,
        SESSION_VIABILITY_RUNNING_NAMESPACE,
        device_key,
        {"started_at": _now_iso(), "checked_by": checked_by},
    )
    if not acquired:
        raise ValueError("Session viability check already in progress for this device")
    await db.commit()
    can_probe = (device.operational_state == DeviceOperationalState.available and device.hold is None) or (
        checked_by == SessionViabilityCheckedBy.recovery and device.operational_state == DeviceOperationalState.offline
    )
    if not can_probe:
        await control_plane_state_store.delete_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
        await db.commit()
        raise ValueError("Session viability checks only run for available devices")
    if not await is_ready_for_use_async(db, device):
        await control_plane_state_store.delete_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
        await db.commit()
        raise ValueError(await readiness_error_detail_async(db, device, action="run a session viability check"))

    attempted_at = _now_iso()
    try:
        config_changed = _clear_session_viability_from_config(device)
        timeout_sec = int(settings_service.get("general.session_viability_timeout_sec"))
        node = device.appium_node
        if not node or not node.observed_running:
            state = await _write_session_viability(
                db,
                device,
                status="failed",
                attempted_at=attempted_at,
                error="Appium node is not running",
                checked_by=checked_by,
            )
            if config_changed:
                await db.commit()
            return state

        locked = await device_locking.lock_device(db, device.id)
        previous_state = locked.operational_state
        await set_operational_state(
            locked,
            DeviceOperationalState.busy,
            reason="Session viability probe running",
            publish_event=True,
        )
        await db.commit()

        capabilities = build_probe_capabilities(await capability_service.get_device_capabilities(db, device))
        ok, error = await probe_session_via_grid(capabilities, timeout_sec, grid_url=node.grid_url)

        state = await _write_session_viability(
            db,
            device,
            status="passed" if ok else "failed",
            attempted_at=attempted_at,
            error=error,
            checked_by=checked_by,
        )

        relocked = await device_locking.lock_device(db, device.id)
        if relocked.operational_state == DeviceOperationalState.busy:
            await set_operational_state(
                relocked,
                await ready_operational_state(db, relocked),
                reason="Session viability probe finished",
                publish_event=True,
            )
            await db.commit()
        else:
            logger.info(
                "Device %s availability changed during probe (now %s); skipping restore",
                device.id,
                relocked.operational_state.value,
            )
            if config_changed:
                await db.commit()
        if not ok and checked_by != SessionViabilityCheckedBy.recovery and _health_failure_handler is not None:
            await _health_failure_handler(
                db,
                device,
                source="session_viability",
                reason=error or "Appium session viability probe failed",
            )
        return state
    except Exception:
        if previous_state in {DeviceOperationalState.available, DeviceOperationalState.offline}:
            relocked = await device_locking.lock_device(db, device.id)
            if relocked.operational_state == DeviceOperationalState.busy:
                if previous_state == DeviceOperationalState.offline:
                    await set_operational_state(relocked, DeviceOperationalState.offline, publish_event=False)
                else:
                    await set_operational_state(
                        relocked,
                        await ready_operational_state(db, relocked),
                        publish_event=False,
                    )
                await db.commit()
        raise
    finally:
        await control_plane_state_store.delete_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, device_key)
        await db.commit()


async def reset_session_viability_control_plane_state(db: AsyncSession) -> None:
    await control_plane_state_store.delete_namespaces(
        db,
        [SESSION_VIABILITY_STATE_NAMESPACE, SESSION_VIABILITY_RUNNING_NAMESPACE],
    )
    await db.commit()


async def get_session_viability_control_plane_state(db: AsyncSession) -> dict[str, Any]:
    return {
        "running": sorted((await control_plane_state_store.get_values(db, SESSION_VIABILITY_RUNNING_NAMESPACE)).keys()),
        "state": await control_plane_state_store.get_values(db, SESSION_VIABILITY_STATE_NAMESPACE),
    }


async def set_session_viability_control_plane_entry(db: AsyncSession, device_key: str, state: dict[str, Any]) -> None:
    await control_plane_state_store.set_value(db, SESSION_VIABILITY_STATE_NAMESPACE, device_key, state)
    await db.commit()


async def _check_due_devices(db: AsyncSession) -> None:
    interval_sec = settings_service.get("general.session_viability_interval_sec")
    stmt = (
        select(Device)
        .where(Device.operational_state == DeviceOperationalState.available, Device.hold.is_(None))
        .options(selectinload(Device.host), selectinload(Device.appium_node))
    )
    result = await db.execute(stmt)
    devices = result.scalars().all()

    for device in devices:
        if await _should_run_scheduled_probe(db, device, interval_sec):
            await run_session_viability_probe(db, device, checked_by=SessionViabilityCheckedBy.scheduled)


async def session_viability_loop() -> None:
    while True:
        try:
            async with observe_background_loop(LOOP_NAME, 60.0).cycle(), async_session() as db:
                await _check_due_devices(db)
        except Exception:
            logger.exception("Session viability loop failed")
        await asyncio.sleep(60)
