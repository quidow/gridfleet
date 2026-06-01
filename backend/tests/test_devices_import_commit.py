import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from httpx import AsyncClient
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
from app.devices.services.portability_import import BundleHashMismatchError, PortabilityImportService
from app.devices.services.verification import VerificationService
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
async def test_commit_creates_device_and_enqueues_verification(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
        db_session, request
    )

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
async def test_commit_rejects_bundle_hash_mismatch(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash="sha256:" + "0" * 64,
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    with pytest.raises(BundleHashMismatchError):
        await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(db_session, request)


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_skips_duplicate_in_bundle_rows(db_session: AsyncSession, seeded_driver_packs: None) -> None:
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
    result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
        db_session, request
    )
    assert result.created == []
    assert len(result.skipped) == 2
    assert all(r.reason == "duplicate in bundle" for r in result.skipped)


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_skips_existing_identity_as_conflict_skip(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
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
    result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
        db_session, request
    )
    assert result.created == []
    assert len(result.skipped) == 1
    assert "identity" in result.skipped[0].reason


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_fails_row_when_host_missing(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    bogus = uuid.uuid4()
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=bogus)],
    )
    result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
        db_session, request
    )
    assert result.created == []
    assert len(result.failed) == 1
    assert "host" in result.failed[0].reason


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_rolls_back_device_when_verification_enqueue_fails(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )

    with patch(
        "app.devices.services.verification.job_queue.create_job",
        side_effect=RuntimeError("boom"),
    ):
        result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
            db_session, request
        )

    assert result.created == []
    assert len(result.failed) == 1
    assert "verification" in result.failed[0].reason.lower() or "boom" in result.failed[0].reason.lower()
    found = (await db_session.execute(select(Device).where(Device.identity_value == "R58"))).scalar_one_or_none()
    assert found is None


@pytest.mark.asyncio
@pytest.mark.db
async def test_import_mapping_forbids_device_field(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    """Server re-parses bundle; mappings carry only target_host_id (no per-row device override)."""
    host = await seed_host_named(db_session, "lab-04")
    with pytest.raises(ValidationError):
        ImportMapping.model_validate({"index": 0, "target_host_id": str(host.id), "device": {"name": "x"}})


@pytest.mark.asyncio
@pytest.mark.db
async def test_import_endpoint_returns_409_on_hash_mismatch(
    client: AsyncClient, db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    body = {
        "bundle": {
            "schema_version": 1,
            "exported_at": "2026-05-23T00:00:00+00:00",
            "devices": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "R58",
                    "name": "Pixel",
                    "device_type": "real_device",
                    "connection_type": "usb",
                    "tags": {},
                    "device_config": {},
                    "test_data": {},
                    "original_host": {"hostname": "lab-04"},
                }
            ],
        },
        "bundle_hash": "sha256:" + "0" * 64,
        "mappings": [{"index": 0, "target_host_id": str(host.id)}],
    }
    response = await client.post("/api/devices/import", json=body)
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.db
async def test_import_endpoint_commits_valid_row(
    client: AsyncClient, db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle_body = {
        "schema_version": 1,
        "exported_at": "2026-05-23T00:00:00+00:00",
        "devices": [
            {
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "identity_scheme": "android_serial",
                "identity_scope": "host",
                "identity_value": "R58",
                "name": "Pixel",
                "device_type": "real_device",
                "connection_type": "usb",
                "tags": {},
                "device_config": {},
                "test_data": {},
                "original_host": {"hostname": "lab-04"},
            }
        ],
    }
    bundle = ExportBundle.model_validate(bundle_body)
    body = {
        "bundle": bundle_body,
        "bundle_hash": compute_bundle_hash(bundle),
        "mappings": [{"index": 0, "target_host_id": str(host.id)}],
    }
    response = await client.post("/api/devices/import", json=body)
    assert response.status_code == 200
    result = response.json()
    assert len(result["created"]) == 1
    assert result["skipped"] == []
    assert result["failed"] == []


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_handles_session_commit_failure_after_savepoint_release(
    db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    """If session.commit() fails after savepoint.commit() succeeded, the per-row exception
    handler must not crash trying to roll back an already-released savepoint."""
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )

    original_commit = db_session.__class__.commit
    call_count = {"n": 0}

    async def flaky_commit(self: AsyncSession) -> None:  # type: ignore[override]
        call_count["n"] += 1
        # First call (the per-row outer commit) fails; subsequent calls (e.g. test teardown) succeed.
        if call_count["n"] == 1:
            raise RuntimeError("outer commit failed")
        return await original_commit(self)

    with patch.object(db_session.__class__, "commit", flaky_commit):
        result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
            db_session, request
        )

    # The row is reported as failed with the outer commit error message, not an InvalidStateError.
    assert result.created == []
    assert len(result.failed) == 1
    assert "outer commit" in result.failed[0].reason.lower()


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_partial_failure_mixed_results(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    """One created, one skipped (conflict), one failed (missing host)."""
    host = await seed_host_named(db_session, "lab-04")
    await seed_existing_device(
        db_session,
        host_id=host.id,
        identity_scheme="android_serial",
        identity_value="CONFLICT",
        identity_scope="host",
    )
    bundle = _bundle(
        [
            _device(identity_value="NEW-1"),
            _device(identity_value="CONFLICT"),
            _device(identity_value="NEW-2"),
        ]
    )
    bogus_host = uuid.uuid4()
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[
            ImportMapping(index=0, target_host_id=host.id),
            ImportMapping(index=1, target_host_id=host.id),
            ImportMapping(index=2, target_host_id=bogus_host),
        ],
    )
    result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
        db_session, request
    )
    assert len(result.created) == 1
    assert result.created[0].index == 0
    assert len(result.skipped) == 1
    assert result.skipped[0].index == 1
    assert len(result.failed) == 1
    assert result.failed[0].index == 2
