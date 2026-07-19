from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.core.database import async_session
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs import queue as job_queue
from app.jobs.models import Job
from app.packs.services import platform_resolver as pack_platform_resolver
from app.verification.services.job_state import (
    new_job,
    public_snapshot,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.devices.models import Device

assert_runnable = pack_platform_resolver.assert_runnable

__all__ = [
    "VerificationService",
]


class VerificationService:
    async def start_verification_job(
        self, data: DeviceVerificationCreate, session_factory: SessionFactory = async_session
    ) -> dict[str, Any]:
        # Gate: ensure pack is runnable before creating verification job
        if data.pack_id is not None and data.platform_id is not None:
            async with session_factory() as db:
                await assert_runnable(db, pack_id=data.pack_id, platform_id=data.platform_id)

        job_uuid = uuid.uuid4()
        async with session_factory() as db:
            row = await job_queue.create_job(
                db,
                kind=JOB_KIND_DEVICE_VERIFICATION,
                payload={"mode": "create", "data": data.model_dump(mode="json")},
                snapshot=new_job(str(job_uuid)),
                max_attempts=1,
                job_id=job_uuid,
            )
        return public_snapshot(row.snapshot)

    async def start_existing_device_verification_job(
        self, device_id: uuid.UUID, data: DeviceVerificationUpdate, session_factory: SessionFactory = async_session
    ) -> dict[str, Any]:
        job_uuid = uuid.uuid4()
        async with session_factory() as db:
            row = await job_queue.create_job(
                db,
                kind=JOB_KIND_DEVICE_VERIFICATION,
                payload={
                    "mode": "update",
                    "device_id": str(device_id),
                    "data": data.model_dump(mode="json", exclude_unset=True),
                },
                snapshot=new_job(str(job_uuid)),
                max_attempts=1,
                job_id=job_uuid,
            )
        return public_snapshot(row.snapshot)

    async def get_verification_job(
        self, job_id: str, session_factory: SessionFactory = async_session
    ) -> dict[str, Any] | None:
        try:
            parsed_job_id = uuid.UUID(job_id)
        except ValueError:
            return None

        async with session_factory() as db:
            row = await db.get(Job, parsed_job_id)
        if row is None or row.kind != JOB_KIND_DEVICE_VERIFICATION:
            return None
        return public_snapshot(row.snapshot)

    async def enqueue_for_device(self, db: AsyncSession, device: Device) -> uuid.UUID:
        """Enqueue a create-mode verification job for an already-built device row.

        Uses the caller's session with ``commit=False`` so the enqueue joins the
        caller's transaction (e.g. the per-row portability-import savepoint).
        """
        job_id = uuid.uuid4()
        data = DeviceVerificationCreate(
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            identity_scheme=device.identity_scheme,
            identity_scope=device.identity_scope,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            name=device.name,
            host_id=device.host_id,
            device_type=device.device_type,
            connection_type=device.connection_type,
        )
        await job_queue.create_job(
            db,
            kind=JOB_KIND_DEVICE_VERIFICATION,
            payload={"mode": "create", "data": data.model_dump(mode="json")},
            snapshot=new_job(str(job_id)),
            max_attempts=1,
            job_id=job_id,
            commit=False,
        )
        return job_id
