import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_401, RESPONSES_404, RESPONSES_422
from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.devices.dependencies import DeviceServicesDep
from app.devices.schemas.device import (
    DeviceVerificationCreate,
    DeviceVerificationUpdate,
)
from app.events import Event
from app.events.dependencies import EventServicesDep
from app.verification.dependencies import VerificationServicesDep
from app.verification.schemas import DeviceVerificationJobRead
from app.verification.services.job_state import public_snapshot

DEVICE_VERIFICATION_ERROR_RESPONSES = {**RESPONSES_401, **RESPONSES_404, **RESPONSES_422}

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
    device = await device_services.crud.get_device(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
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
    job = await verification_services.service.get_verification_job(job_id, session_factory=session_factory)
    if job is None:
        raise HTTPException(status_code=404, detail="Verification job not found")
    return job


@router.get("/jobs/{job_id}/events")
async def stream_device_verification_job_events(
    job_id: str,
    request: Request,
    db: DbDep,
    event_services: EventServicesDep,
    verification_services: VerificationServicesDep,
) -> EventSourceResponse:
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    initial_job = await verification_services.service.get_verification_job(job_id, session_factory=session_factory)
    if initial_job is None:
        raise HTTPException(status_code=404, detail="Verification job not found")

    queue = event_services.subscriber.subscribe()

    async def generate() -> AsyncGenerator[dict[str, str], None]:
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
