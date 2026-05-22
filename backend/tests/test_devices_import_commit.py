import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device
from app.devices.schemas.portability import (
    ExportBundle,
    ExportedDevice,
    ImportCommitRequest,
    ImportMapping,
    OriginalHost,
)
from app.devices.services.portability_hash import compute_bundle_hash
from app.devices.services.portability_import import BundleHashMismatchError, commit_import
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs.models import Job
from tests.helpers import seed_existing_device, seed_host_named


def _bundle(devices: list[ExportedDevice]) -> ExportBundle:
    return ExportBundle(
        schema_version=1,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        devices=devices,
    )


def _device(identity_value: str = "R58", hostname: str = "lab-04") -> ExportedDevice:
    return ExportedDevice(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity_value,
        name="Pixel",
        device_type="real_device",
        connection_type="usb",
        original_host=OriginalHost(hostname=hostname),
    )


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_creates_device_and_enqueues_verification(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    result = await commit_import(db_session, request)

    assert len(result.created) == 1
    assert result.failed == []
    assert result.skipped == []
    device_id = result.created[0].device_id

    device = (await db_session.execute(select(Device).where(Device.id == device_id))).scalar_one()
    assert device.host_id == host.id
    assert device.identity_value == "R58"
    assert device.operational_state.value == "offline"

    jobs = (await db_session.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_VERIFICATION))).scalars().all()
    assert len(jobs) == 1


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_rejects_bundle_hash_mismatch(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash="sha256:" + "0" * 64,
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    with pytest.raises(BundleHashMismatchError):
        await commit_import(db_session, request)


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_skips_duplicate_in_bundle_rows(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device(identity_value="X"), _device(identity_value="X")])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[
            ImportMapping(index=0, target_host_id=host.id),
            ImportMapping(index=1, target_host_id=host.id),
        ],
    )
    result = await commit_import(db_session, request)
    assert result.created == []
    assert len(result.skipped) == 2
    assert all(r.reason == "duplicate in bundle" for r in result.skipped)


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_skips_existing_identity_as_conflict_skip(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "lab-04")
    await seed_existing_device(
        db_session,
        host_id=host.id,
        identity_scheme="android_serial",
        identity_value="R58",
        identity_scope="host",
    )
    bundle = _bundle([_device(identity_value="R58")])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    result = await commit_import(db_session, request)
    assert result.created == []
    assert len(result.skipped) == 1
    assert "identity" in result.skipped[0].reason


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_fails_row_when_host_missing(db_session: AsyncSession) -> None:
    await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    bogus = uuid.uuid4()
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=bogus)],
    )
    result = await commit_import(db_session, request)
    assert result.created == []
    assert len(result.failed) == 1
    assert "host" in result.failed[0].reason


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_rolls_back_device_when_verification_enqueue_fails(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )

    with patch(
        "app.devices.services.portability_import.job_queue.create_job",
        side_effect=RuntimeError("boom"),
    ):
        result = await commit_import(db_session, request)

    assert result.created == []
    assert len(result.failed) == 1
    assert "verification" in result.failed[0].reason.lower() or "boom" in result.failed[0].reason.lower()
    found = (await db_session.execute(select(Device).where(Device.identity_value == "R58"))).scalar_one_or_none()
    assert found is None


@pytest.mark.asyncio
@pytest.mark.db
async def test_import_mapping_forbids_device_field(db_session: AsyncSession) -> None:
    """Server re-parses bundle; mappings carry only target_host_id (no per-row device override)."""
    host = await seed_host_named(db_session, "lab-04")
    with pytest.raises(ValidationError):
        ImportMapping.model_validate({"index": 0, "target_host_id": str(host.id), "device": {"name": "x"}})
