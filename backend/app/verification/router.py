from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_401, RESPONSES_404, RESPONSES_409, RESPONSES_422
from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.core.http_errors import found_or_404
from app.devices.dependencies import DeviceServicesDep
from app.devices.schemas.device import (
    DeviceVerificationCreate,
    DeviceVerificationUpdate,
)
from app.events.dependencies import EventServicesDep
from app.lifecycle.services.operator_node import operator_stop_active
from app.sessions.service import device_has_running_session
from app.verification.dependencies import VerificationServicesDep
from app.verification.schemas import DeviceVerificationJobRead
from app.verification.services.job_state import public_snapshot

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from app.events import Event

DEVICE_VERIFICATION_ERROR_RESPONSES = {**RESPONSES_401, **RESPONSES_404, **RESPONSES_409, **RESPONSES_422}

router = APIRouter(prefix="/api/verification", tags=["verification"], responses=DEVICE_VERIFICATION_ERROR_RESPONSES)


async def _read_queue_event(queue: asyncio.Queue[Event]) -> Event:
    get_task = asyncio.create_task(queue.get())
    try:
        return await get_task
    finally:
        if not get_task.done():
            get_task.cancel()
            _ = await asyncio.gather(get_task, return_exceptions=True)


@router.post("/jobs", response_model=DeviceVerificationJobRead, status_code=202)
async def create_device_verification_job(
    data: DeviceVerificationCreate,
    db: DbDep,
    verification_services: VerificationServicesDep,
) -> dict[str, Any]:
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    try:
        return await verification_services.service.start_verification_job(data, session_factory=session_factory)
    except (PackUnavailableError, PackDisabledError, PackDrainingError, PlatformRemovedError) as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/devices/{device_id}/jobs", response_model=DeviceVerificationJobRead, status_code=202)
async def create_existing_device_verification_job(
    device_id: uuid.UUID,
    data: DeviceVerificationUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
    verification_services: VerificationServicesDep,
) -> dict[str, Any]:
    found_or_404(await device_services.crud.get_device(db, device_id), "Device not found")
    if await device_has_running_session(db, device_id):
        raise HTTPException(
            status_code=409,
            detail="Cannot verify a device with a live session; end the session first",
        )
    # A re-verify runs through the node-start path (request_start), which revokes the
    # sticky operator:stop — silently reviving a device the operator deliberately stopped
    # and re-enabling auto-recovery (N13b). Operator stop is lifted only by an operator
    # start, so refuse the verify here; the preparation step backstops the enqueue→run race.
    if await operator_stop_active(db, device_id):
        raise HTTPException(
            status_code=409,
            detail="Cannot verify an operator-stopped device; start the node first",
        )
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    return await verification_services.service.start_existing_device_verification_job(
        device_id,
        data,
        session_factory=session_factory,
    )


@router.get("/jobs/{job_id}", response_model=DeviceVerificationJobRead)
async def get_device_verification_job(
    job_id: str, db: DbDep, verification_services: VerificationServicesDep
) -> dict[str, Any]:
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    return found_or_404(
        await verification_services.service.get_verification_job(job_id, session_factory=session_factory),
        "Verification job not found",
    )


@router.get("/jobs/{job_id}/events")
async def stream_device_verification_job_events(
    job_id: str,
    request: Request,
    db: DbDep,
    event_services: EventServicesDep,
    verification_services: VerificationServicesDep,
) -> EventSourceResponse:
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    initial_job = found_or_404(
        await verification_services.service.get_verification_job(job_id, session_factory=session_factory),
        "Verification job not found",
    )

    queue = event_services.subscriber.subscribe()

    async def generate() -> AsyncGenerator[dict[str, str]]:
        try:
            yield {
                "event": "device.verification.updated",
                "data": json.dumps(initial_job),
            }
            if initial_job["status"] in {"completed", "failed"}:
                return

            while True:
                if await request.is_disconnected():
                    return
                event = await _read_queue_event(queue)
                if event.type != "device.verification.updated":
                    continue
                if str(event.data.get("job_id")) != job_id:
                    continue

                payload = public_snapshot(event.data)
                yield {
                    "event": event.type,
                    "id": event.id,
                    "data": json.dumps(payload),
                }
                if payload["status"] in {"completed", "failed"}:
                    return
        finally:
            event_services.subscriber.unsubscribe(queue)

    return EventSourceResponse(generate())
