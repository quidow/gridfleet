from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.errors import AgentCallError
from app.models.job import Job
from app.services import control_plane_state_store, host_service
from app.services.agent_operations import ensure_tools as ensure_agent_tools
from app.services.job_queue import (
    JOB_KIND_HOST_TOOLS_ENSURE,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
)
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.host import Host

HOST_TOOLS_ENSURE_NAMESPACE = "host.tools.ensure"


def utcnow() -> datetime:
    return datetime.now(UTC)


def configured_tool_versions() -> tuple[str | None, str | None]:
    appium_version = settings_service.get("appium.target_version")
    selenium_version = settings_service.get("grid.selenium_jar_version")
    appium_target = appium_version.strip() if isinstance(appium_version, str) else ""
    selenium_target = selenium_version.strip() if isinstance(selenium_version, str) else ""
    return appium_target or None, selenium_target or None


def _job_payload(host: Host) -> dict[str, Any]:
    appium_version, selenium_version = configured_tool_versions()
    return {
        "host_id": str(host.id),
        "appium_version": appium_version,
        "selenium_jar_version": selenium_version,
    }


def _job_snapshot(
    *,
    job_id: uuid.UUID,
    host: Host,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    appium_version, selenium_version = configured_tool_versions()
    return {
        "job_id": str(job_id),
        "status": status,
        "host_id": str(host.id),
        "hostname": host.hostname,
        "target_versions": {
            "appium": appium_version,
            "selenium_jar": selenium_version,
        },
        "result": result,
        "error": error,
        "started_at": started_at or utcnow().isoformat(),
        "finished_at": finished_at,
    }


async def store_tool_ensure_result(db: AsyncSession, host_id: uuid.UUID, payload: dict[str, Any]) -> None:
    await control_plane_state_store.set_value(
        db,
        HOST_TOOLS_ENSURE_NAMESPACE,
        str(host_id),
        {
            "recorded_at": utcnow().isoformat(),
            **payload,
        },
    )


async def _ensure_host_tools_versions(
    db: AsyncSession,
    host: Host,
    *,
    appium_version: str | None,
    selenium_version: str | None,
) -> dict[str, Any] | None:
    if appium_version is None and selenium_version is None:
        return None
    result = await ensure_agent_tools(
        host.ip,
        host.agent_port,
        appium_version=appium_version,
        selenium_jar_version=selenium_version,
    )
    await store_tool_ensure_result(db, host.id, {"result": result})
    await db.commit()
    return result


async def ensure_host_tools(db: AsyncSession, host: Host) -> dict[str, Any] | None:
    appium_version, selenium_version = configured_tool_versions()
    return await _ensure_host_tools_versions(
        db,
        host,
        appium_version=appium_version,
        selenium_version=selenium_version,
    )


async def start_host_tool_ensure_job(db: AsyncSession, host: Host) -> dict[str, Any]:
    job_id = uuid.uuid4()
    snapshot = _job_snapshot(job_id=job_id, host=host, status=JOB_STATUS_PENDING)
    row = await create_job(
        db,
        kind=JOB_KIND_HOST_TOOLS_ENSURE,
        payload=_job_payload(host),
        snapshot=snapshot,
        job_id=job_id,
    )
    return copy.deepcopy(row.snapshot)


async def get_host_tool_ensure_job(db: AsyncSession, job_id: uuid.UUID) -> dict[str, Any] | None:
    row = await get_job(db, job_id)
    if row is None or row.kind != JOB_KIND_HOST_TOOLS_ENSURE:
        return None
    return copy.deepcopy(row.snapshot)


async def run_persisted_host_tool_ensure_job(
    job_id: str,
    payload: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    parsed_job_id = uuid.UUID(job_id)
    host_id = uuid.UUID(str(payload["host_id"]))
    async with session_factory() as db:
        row = await db.get(Job, parsed_job_id)
        host = await host_service.get_host(db, host_id)
        if row is None or host is None:
            return
        started_at = utcnow().isoformat()
        row.status = JOB_STATUS_RUNNING
        snapshot = copy.deepcopy(row.snapshot)
        snapshot["status"] = JOB_STATUS_RUNNING
        snapshot["started_at"] = started_at
        snapshot["error"] = None
        row.snapshot = snapshot
        await db.commit()

    try:
        async with session_factory() as db:
            host = await host_service.get_host(db, host_id)
            row = await db.get(Job, parsed_job_id)
            if host is None or row is None:
                return
            appium_version = payload.get("appium_version")
            selenium_version = payload.get("selenium_jar_version")
            result = (
                await _ensure_host_tools_versions(
                    db,
                    host,
                    appium_version=appium_version if isinstance(appium_version, str) else None,
                    selenium_version=selenium_version if isinstance(selenium_version, str) else None,
                )
                or {}
            )
            finished_at = utcnow().isoformat()
            row.status = JOB_STATUS_COMPLETED
            snapshot = copy.deepcopy(row.snapshot)
            snapshot["status"] = JOB_STATUS_COMPLETED
            snapshot["result"] = result
            snapshot["finished_at"] = finished_at
            row.snapshot = snapshot
            row.completed_at = utcnow()
            await db.commit()
    except (AgentCallError, OSError, ValueError) as exc:
        async with session_factory() as db:
            row = await db.get(Job, parsed_job_id)
            if row is None:
                return
            row.status = JOB_STATUS_FAILED
            snapshot = copy.deepcopy(row.snapshot)
            snapshot["status"] = JOB_STATUS_FAILED
            snapshot["error"] = str(exc)
            snapshot["finished_at"] = utcnow().isoformat()
            row.snapshot = snapshot
            row.completed_at = utcnow()
            await store_tool_ensure_result(db, host_id, {"error": str(exc)})
            await db.commit()
