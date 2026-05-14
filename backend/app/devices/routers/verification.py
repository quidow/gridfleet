import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from app.core.dependencies import DbDep
from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.devices.schemas.device import (
    DeviceVerificationCreate,
    DeviceVerificationJobRead,
    DeviceVerificationUpdate,
)
from app.devices.services import service as device_service
from app.devices.services import verification as device_verification
from app.devices.services.verification_job_state import public_snapshot
from app.events import Event, event_bus

router = APIRouter()


async def _read_queue_event(queue: asyncio.Queue[Event]) -> Event:
    get_task = asyncio.create_task(queue.get())
    try:
        return await get_task
    finally:
        if not get_task.done():
            get_task.cancel()
            _ = await asyncio.gather(get_task, return_exceptions=True)


@router.post("/verification-jobs", response_model=DeviceVerificationJobRead, status_code=202)
async def create_device_verification_job(
    data: DeviceVerificationCreate,
    db: DbDep,
) -> dict[str, Any]:
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    try:
        return await device_verification.start_verification_job(data, session_factory=session_factory)
    except (PackUnavailableError, PackDisabledError, PackDrainingError, PlatformRemovedError) as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/{device_id}/verification-jobs", response_model=DeviceVerificationJobRead, status_code=202)
async def create_existing_device_verification_job(
    device_id: uuid.UUID,
    data: DeviceVerificationUpdate,
    db: DbDep,
) -> dict[str, Any]:
    device = await device_service.get_device(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    return await device_verification.start_existing_device_verification_job(
        device_id,
        data,
        session_factory=session_factory,
    )


@router.get("/verification-jobs/{job_id}", response_model=DeviceVerificationJobRead)
async def get_device_verification_job(job_id: str, db: DbDep) -> dict[str, Any]:
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    job = await device_verification.get_verification_job(job_id, session_factory=session_factory)
    if job is None:
        raise HTTPException(status_code=404, detail="Verification job not found")
    return job


@router.get("/verification-jobs/{job_id}/events")
async def stream_device_verification_job_events(
    job_id: str,
    request: Request,
    db: DbDep,
) -> EventSourceResponse:
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    initial_job = await device_verification.get_verification_job(job_id, session_factory=session_factory)
    if initial_job is None:
        raise HTTPException(status_code=404, detail="Verification job not found")

    queue = event_bus.subscribe()

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
            event_bus.unsubscribe(queue)

    return EventSourceResponse(generate())
