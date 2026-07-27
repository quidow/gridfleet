import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.devices.models import Device, DeviceGroup, DeviceGroupMemberOf, DeviceGroupMembership, GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs.models import Job
from app.portability.schemas import (
    ExportBundle,
    ExportedDevice,
    ExportedDeviceGroup,
    ImportCommitRequest,
    ImportMapping,
    OriginalHost,
)
from app.portability.services import import_bundle as import_bundle_module
from app.portability.services.hash import compute_bundle_hash
from app.portability.services.import_bundle import (
    BundleHashMismatchError,
    PortabilityImportService,
    UnknownGroupReferenceError,
)
from app.verification.services.service import VerificationService
from tests.concurrency.group_lock_helpers import pin_statement_listener
from tests.fakes.session_factory import RecordingSessionFactory
from tests.helpers import seed_existing_device, seed_host_named

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _bundle(devices: list[ExportedDevice], groups: list[ExportedDeviceGroup] | None = None) -> ExportBundle:
    return ExportBundle(
        schema_version=2,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        groups=groups or [],
        devices=devices,
    )


def _device(
    identity_value: str = "R58",
    hostname: str = "lab-04",
    static_groups: list[str] | None = None,
) -> ExportedDevice:
    return ExportedDevice(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity_value,
        name="Pixel",
        device_type="real_device",
        connection_type="usb",
        static_groups=static_groups or [],
        original_host=OriginalHost(hostname=hostname),
    )


def _static_group(key: str) -> ExportedDeviceGroup:
    return ExportedDeviceGroup(key=key, name=key.replace("-", " "), group_type=GroupType.static)


def _dynamic_group(key: str, member_of: list[str]) -> ExportedDeviceGroup:
    return ExportedDeviceGroup(
        key=key,
        name=key.replace("-", " "),
        group_type=GroupType.dynamic,
        filters=DeviceGroupFilters(member_of=member_of),
    )


async def _committed_group_keys(session_maker: async_sessionmaker[AsyncSession]) -> dict[str, DeviceGroup]:
    """Read device groups back through a fresh session so uncommitted state cannot satisfy the assertion."""
    async with session_maker() as verify_session:
        rows = (await verify_session.execute(select(DeviceGroup))).scalars().all()
        return {row.key: row for row in rows}


class _FailingEnqueuer:
    """Delegates to a real VerificationService, except for one named identity."""

    def __init__(self, real: VerificationService, fail_identity_value: str) -> None:
        self._real = real
        self._fail_identity_value = fail_identity_value

    async def enqueue_for_device(self, db: AsyncSession, device: Device) -> uuid.UUID:
        if device.identity_value == self._fail_identity_value:
            raise RuntimeError("simulated verification enqueue failure")
        return await self._real.enqueue_for_device(db, device)


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_creates_device_and_enqueues_verification(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)

    assert len(result.created) == 1
    assert result.failed == []
    assert result.skipped == []
    device_id = result.created[0].device_id

    device = (await db_session.execute(select(Device).where(Device.id == device_id))).scalar_one()
    assert device.host_id == host.id
    assert device.identity_value == "R58"
    assert device.operational_state_last_emitted.value == "offline"

    jobs = (await db_session.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_VERIFICATION))).scalars().all()
    assert len(jobs) == 1


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_rejects_bundle_hash_mismatch(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash="sha256:" + "0" * 64,
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    with pytest.raises(BundleHashMismatchError):
        await service.commit_import(request)


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_skips_duplicate_in_bundle_rows(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
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
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)
    assert result.created == []
    assert len(result.skipped) == 2
    assert all(r.reason == "duplicate in bundle" for r in result.skipped)


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_skips_existing_identity_as_conflict_skip(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
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
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)
    assert result.created == []
    assert len(result.skipped) == 1
    assert "identity" in result.skipped[0].reason


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_fails_row_when_host_missing(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    bogus = uuid.uuid4()
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=bogus)],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)
    assert result.created == []
    assert len(result.failed) == 1
    assert "host" in result.failed[0].reason


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_rolls_back_device_when_verification_enqueue_fails(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle([_device()])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )

    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    with patch(
        "app.verification.services.service.job_queue.create_job",
        side_effect=RuntimeError("boom"),
    ):
        result = await service.commit_import(request)

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
            "schema_version": 2,
            "exported_at": "2026-05-23T00:00:00+00:00",
            "groups": [],
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
                    "static_groups": [],
                    "device_config": {},
                    "test_data": {},
                    "original_host": {"hostname": "lab-04"},
                }
            ],
        },
        "bundle_hash": "sha256:" + "0" * 64,
        "mappings": [{"index": 0, "target_host_id": str(host.id)}],
    }
    response = await client.post("/api/portability/import", json=body)
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.db
async def test_import_endpoint_commits_valid_row(
    client: AsyncClient, db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle_body = {
        "schema_version": 2,
        "exported_at": "2026-05-23T00:00:00+00:00",
        "groups": [],
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
                "static_groups": [],
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
    response = await client.post("/api/portability/import", json=body)
    assert response.status_code == 200
    result = response.json()
    assert len(result["created"]) == 1
    assert result["skipped"] == []
    assert result["failed"] == []


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_savepoint_contains_a_failed_row_to_itself_and_its_job(
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """Row two's verification enqueue fails; only its device and job roll back.

    Definitions, the device batch, and the membership batch each own a
    separate session opened from the injected ``session_factory`` -- this pins
    that boundary shape (distinct session identities, one nested transaction
    per attempted row) alongside the row-two/job-two rollback it protects.
    Replaces the old ``session.commit``-monkeypatch tests, which assumed a
    single shared session the command no longer holds.
    """
    async with db_session_maker() as seed_db:
        host = await seed_host_named(seed_db, "lab-04")
        host_id = host.id

    bundle = _bundle(
        [
            _device(identity_value="ROW-1", static_groups=["shelf-a"]),
            _device(identity_value="ROW-2", static_groups=["shelf-a"]),
            _device(identity_value="ROW-3", static_groups=["shelf-a"]),
        ],
        groups=[_static_group("shelf-a")],
    )
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[
            ImportMapping(index=0, target_host_id=host_id),
            ImportMapping(index=1, target_host_id=host_id),
            ImportMapping(index=2, target_host_id=host_id),
        ],
    )

    factory = RecordingSessionFactory(db_session_maker, statement_pinner=pin_statement_listener)
    try:
        service = PortabilityImportService(
            verification_enqueuer=_FailingEnqueuer(VerificationService(), "ROW-2"),
            session_factory=factory,
        )
        result = await service.commit_import(request)
    finally:
        factory.close()

    assert [r.index for r in result.created] == [0, 2]
    assert len(result.failed) == 1
    assert result.failed[0].index == 1
    assert "verification" in result.failed[0].reason.lower()

    async with db_session_maker() as verify:
        surviving = (
            (await verify.execute(select(Device.identity_value).order_by(Device.identity_value))).scalars().all()
        )
        assert surviving == ["ROW-1", "ROW-3"], "row two must not persist"

        jobs = (await verify.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_VERIFICATION))).scalars().all()
        job_identities = {job.payload["data"]["identity_value"] for job in jobs}
        assert job_identities == {"ROW-1", "ROW-3"}, "row two's verification job must not persist"

        group = (await verify.execute(select(DeviceGroup).where(DeviceGroup.key == "shelf-a"))).scalar_one()
        memberships = (
            (await verify.execute(select(DeviceGroupMembership).where(DeviceGroupMembership.group_id == group.id)))
            .scalars()
            .all()
        )
        assert len(memberships) == 2, "only the two created rows' memberships may survive"

    # Boundary shape: a read for validate, one definitions transaction, one
    # device-batch transaction (all three rows fit in one
    # DEVICE_IMPORT_BATCH_SIZE batch), and one membership-batch transaction --
    # four distinct sessions, three of them opened via session_factory.begin().
    assert len(factory.sessions) == 4
    assert len({id(session) for session in factory.sessions}) == 4, "every session must be a distinct object"
    assert factory.begun == 3

    device_batch_statements = factory.statements_for(2)
    savepoint_opens = [s for s in device_batch_statements if s.startswith("savepoint")]
    assert len(savepoint_opens) == 3, (
        f"expected one nested transaction per attempted device row: {device_batch_statements}"
    )


@pytest.mark.asyncio
@pytest.mark.db
async def test_a_failure_that_escapes_the_row_helper_only_rolls_back_its_own_batch(
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """DEVICE_IMPORT_BATCH_SIZE + 1 valid rows; an outer failure fires after batch one commits.

    This distinguishes batch durability from row savepoint containment: batch
    one's ``session_factory.begin()`` block has already exited (and committed)
    before batch two's failure fires, so its rows survive independently of
    batch two's rollback -- a completed batch is a crash-durability unit, not
    the same thing as a single row's savepoint. The failure is injected past
    ``_insert_row_with_savepoint``'s own try/except (which is what the
    previous test pins) to simulate a bug that escapes the per-row contract
    entirely, rather than another documented per-row failure.
    """
    async with db_session_maker() as seed_db:
        host = await seed_host_named(seed_db, "lab-04")
        host_id = host.id

    total_rows = import_bundle_module.DEVICE_IMPORT_BATCH_SIZE + 1
    devices = [_device(identity_value=f"BATCH-{i:04d}") for i in range(total_rows)]
    bundle = _bundle(devices)
    mappings = [ImportMapping(index=i, target_host_id=host_id) for i in range(total_rows)]
    request = ImportCommitRequest(bundle=bundle, bundle_hash=compute_bundle_hash(bundle), mappings=mappings)

    real_insert = PortabilityImportService._insert_row_with_savepoint
    last_index = total_rows - 1

    async def _flaky_insert(
        self: PortabilityImportService,
        db: AsyncSession,
        idx: int,
        row: object,
        mapping: object,
    ) -> object:
        if idx == last_index:
            raise RuntimeError("simulated outer failure escaping the row helper")
        return await real_insert(self, db, idx, row, mapping)  # type: ignore[arg-type]

    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    with (
        patch.object(PortabilityImportService, "_insert_row_with_savepoint", _flaky_insert),
        pytest.raises(RuntimeError, match="simulated outer failure"),
    ):
        await service.commit_import(request)

    async with db_session_maker() as verify:
        surviving = (
            (await verify.execute(select(Device.identity_value).where(Device.identity_value.like("BATCH-%"))))
            .scalars()
            .all()
        )
    assert len(surviving) == import_bundle_module.DEVICE_IMPORT_BATCH_SIZE, (
        "the first, already-committed batch must remain durable"
    )
    assert f"BATCH-{last_index:04d}" not in surviving, "the row in the failed second batch must not persist"


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_persists_groups_when_every_device_row_is_skipped(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """Group definitions are what the operator asked for; they must survive an all-duplicate device set."""
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle(
        [
            _device(identity_value="DUPE", static_groups=["shelf-a"]),
            _device(identity_value="DUPE", static_groups=["shelf-a"]),
        ],
        groups=[_static_group("shelf-a")],
    )
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[
            ImportMapping(index=0, target_host_id=host.id),
            ImportMapping(index=1, target_host_id=host.id),
        ],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)

    assert result.created == []
    assert len(result.skipped) == 2
    assert all(r.reason == "duplicate in bundle" for r in result.skipped)

    persisted = await _committed_group_keys(db_session_maker)
    assert "shelf-a" in persisted
    assert persisted["shelf-a"].group_type == GroupType.static


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_persists_groups_for_bundle_with_no_devices(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    bundle = _bundle([], groups=[_static_group("shelf-a"), _static_group("shelf-b")])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)

    assert result.created == []
    assert result.skipped == []
    assert result.failed == []

    persisted = await _committed_group_keys(db_session_maker)
    assert set(persisted) == {"shelf-a", "shelf-b"}


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_persists_static_groups_and_memberships(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle(
        [_device(identity_value="R58", static_groups=["shelf-a"])],
        groups=[_static_group("shelf-a")],
    )
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)
    assert len(result.created) == 1
    device_id = result.created[0].device_id

    persisted = await _committed_group_keys(db_session_maker)
    assert set(persisted) == {"shelf-a"}
    async with db_session_maker() as verify_session:
        memberships = (await verify_session.execute(select(DeviceGroupMembership))).scalars().all()
    assert [(m.group_id, m.device_id) for m in memberships] == [(persisted["shelf-a"].id, device_id)]


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_dedupes_a_repeated_static_group_key(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """A key listed twice yields one membership row and no skip entries.

    This pins the end state, not the dedup validator — ``ON CONFLICT DO NOTHING``
    would collapse the duplicate on its own, so this passes with the validator
    removed. The validator's actual job is keeping duplicate ``MembershipSkip``
    entries out of the report; ``test_exported_device_dedupes_static_groups``
    guards that directly.
    """
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle(
        [_device(identity_value="R58", static_groups=["shelf-a", "shelf-a"])],
        groups=[_static_group("shelf-a")],
    )
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)
    assert len(result.created) == 1
    assert result.memberships_skipped == []

    persisted = await _committed_group_keys(db_session_maker)
    async with db_session_maker() as verify_session:
        memberships = (await verify_session.execute(select(DeviceGroupMembership))).scalars().all()
    assert [(m.group_id, m.device_id) for m in memberships] == [(persisted["shelf-a"].id, result.created[0].device_id)]


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_leaves_no_open_transaction_when_nothing_is_staged(
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """A bundle that stages no memberships must not leave any session mid-transaction.

    ``validate_bundle``'s read session and the definitions session are both
    exited (closed) before ``commit_import`` returns. A session surviving open
    to request teardown sits idle holding back the xmin horizon.
    """
    bundle = _bundle([], groups=[_static_group("shelf-a")])
    request = ImportCommitRequest(bundle=bundle, bundle_hash=compute_bundle_hash(bundle), mappings=[])
    factory = RecordingSessionFactory(db_session_maker)
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=factory)
    await service.commit_import(request)

    assert factory.sessions, "the command must have opened at least one session"
    assert factory.open_transactions() == [], "no session opened by commit_import may still be in a transaction"


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_stages_no_memberships_when_the_plan_is_empty(
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """A device in no bundle group must cost membership staging nothing.

    The bundle defines a static group and imports a device that does not belong
    to it, so ``_stage_static_memberships`` plans no rows. It must issue no
    membership INSERT and no per-batch device lock, and leave no session
    behind mid-transaction.
    """
    async with db_session_maker() as seed_db:
        host = await seed_host_named(seed_db, "lab-04")
        host_id = host.id
    bundle = _bundle([_device(static_groups=[])], groups=[_static_group("shelf-a")])
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host_id)],
    )

    factory = RecordingSessionFactory(db_session_maker, statement_pinner=pin_statement_listener)
    try:
        service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=factory)
        result = await service.commit_import(request)
    finally:
        factory.close()

    statements = [s for index in range(len(factory.sessions)) for s in factory.statements_for(index)]
    assert len(result.created) == 1
    assert not any("device_group_memberships" in statement for statement in statements), statements
    assert not any("from devices" in statement and "for key share" in statement for statement in statements), statements
    assert factory.open_transactions() == [], "an empty membership plan left a session mid-transaction"


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_bounds_membership_locks_to_batches(
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each membership batch locks only the device ids it is about to reference.

    With ``MEMBERSHIP_BATCH_SIZE`` forced to 1, two devices produce two batches
    and therefore two separate ``FOR KEY SHARE`` acquisitions, each committed
    before the next begins — rather than one that holds every device in the
    bundle until the last row lands. The ordering clause is what keeps two
    concurrent imports from deadlocking against each other.
    """
    async with db_session_maker() as seed_db:
        host = await seed_host_named(seed_db, "lab-04")
        host_id = host.id
    bundle = _bundle(
        [
            _device(identity_value="batch-1", static_groups=["shelf-a"]),
            _device(identity_value="batch-2", static_groups=["shelf-a"]),
        ],
        groups=[_static_group("shelf-a")],
    )
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[
            ImportMapping(index=0, target_host_id=host_id),
            ImportMapping(index=1, target_host_id=host_id),
        ],
    )
    monkeypatch.setattr(import_bundle_module, "MEMBERSHIP_BATCH_SIZE", 1, raising=False)

    factory = RecordingSessionFactory(db_session_maker, statement_pinner=pin_statement_listener)
    try:
        service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=factory)
        result = await service.commit_import(request)
    finally:
        factory.close()

    statements = [s for index in range(len(factory.sessions)) for s in factory.statements_for(index)]
    assert len(result.created) == 2
    device_locks = [
        statement for statement in statements if "from devices" in statement and "for key share" in statement
    ]
    assert len(device_locks) == 2, statements
    assert all("order by devices.id" in statement for statement in device_locks), device_locks
    assert not any("from device_groups" in statement and "for key share" in statement for statement in statements), (
        statements
    )


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_persists_static_and_dynamic_groups(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    host = await seed_host_named(db_session, "lab-04")
    bundle = _bundle(
        [_device(identity_value="R58", static_groups=["shelf-a"])],
        groups=[_static_group("shelf-a"), _dynamic_group("rack-roll-up", member_of=["shelf-a"])],
    )
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)
    assert len(result.created) == 1

    persisted = await _committed_group_keys(db_session_maker)
    assert set(persisted) == {"shelf-a", "rack-roll-up"}
    assert persisted["rack-roll-up"].group_type == GroupType.dynamic
    assert persisted["rack-roll-up"].filters is None
    async with db_session_maker() as verify:
        edges = (await verify.execute(select(DeviceGroupMemberOf))).scalars().all()
    assert {(edge.dynamic_group_id, edge.static_group_id) for edge in edges} == {
        (persisted["rack-roll-up"].id, persisted["shelf-a"].id)
    }


@pytest.mark.asyncio
@pytest.mark.db
@pytest.mark.parametrize(
    ("groups", "unknown_key"),
    [
        pytest.param(
            [_dynamic_group("rack-roll-up", member_of=["missing-shelf"])],
            "missing-shelf",
            id="key_not_in_bundle",
        ),
        pytest.param(
            [
                _dynamic_group("rack-a", member_of=[]),
                _dynamic_group("rack-roll-up", member_of=["rack-a"]),
            ],
            "rack-a",
            id="key_names_a_dynamic_group",
        ),
    ],
)
async def test_commit_rejects_unresolvable_member_of_before_writing_definitions(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
    groups: list[ExportedDeviceGroup],
    unknown_key: str,
) -> None:
    """A ``member_of`` key that is unknown, or that names a dynamic (not static)
    group in the bundle, must fail validation before any group definition row
    is written — never surface as a partial insert or an FK violation."""
    bundle = _bundle([], groups=groups)
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[],
    )
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    with pytest.raises(UnknownGroupReferenceError) as exc_info:
        await service.commit_import(request)
    assert unknown_key in exc_info.value.keys

    persisted = await _committed_group_keys(db_session_maker)
    assert persisted == {}


@pytest.mark.asyncio
@pytest.mark.db
async def test_commit_partial_failure_mixed_results(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
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
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=db_session_maker)
    result = await service.commit_import(request)
    assert len(result.created) == 1
    assert result.created[0].index == 0
    assert len(result.skipped) == 1
    assert result.skipped[0].index == 1
    assert len(result.failed) == 1
    assert result.failed[0].index == 2


@pytest.mark.asyncio
@pytest.mark.db
async def test_import_endpoint_returns_409_when_a_group_key_is_created_concurrently(
    client: AsyncClient, db_session: AsyncSession, seeded_driver_packs: None
) -> None:
    """A group key that appears between validation and the insert is a 409, not a 500.

    ``_load_existing_group_keys`` takes ``FOR UPDATE``, but row locks cannot reserve
    keys that are not yet in the table — on the normal path (all keys new) it locks
    nothing, so two operators committing the same bundle both pass validation. Patch
    the pre-check to return empty, which is exactly what the loser of that race sees,
    and assert the unique-index violation surfaces as the documented 409.

    Only the *pre-check* is stubbed. ``commit_import``'s ``IntegrityError`` handler
    calls the same helper again, through a fresh session, to name the keys that
    actually collided, and that call must run for real — stubbing it too would
    assert against a fallback rather than against the re-read.
    """
    await seed_host_named(db_session, "lab-04")
    db_session.add(DeviceGroup(key="lab-fleet", name="lab fleet", group_type=GroupType.static))
    await db_session.commit()

    bundle_body = {
        "schema_version": 2,
        "exported_at": "2026-05-23T00:00:00+00:00",
        "groups": [{"key": "lab-fleet", "name": "lab fleet", "group_type": "static", "filters": None}],
        "devices": [],
    }
    bundle = ExportBundle.model_validate(bundle_body)
    body = {"bundle": bundle_body, "bundle_hash": compute_bundle_hash(bundle), "mappings": []}

    real_load = import_bundle_module._load_existing_group_keys
    calls = 0

    async def _sees_no_existing_keys(*args: object, **kwargs: object) -> set[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return set()
        return await real_load(*args, **kwargs)  # type: ignore[arg-type]

    with patch("app.portability.services.import_bundle._load_existing_group_keys", _sees_no_existing_keys):
        response = await client.post("/api/portability/import", json=body)

    assert response.status_code == 409, response.text
    assert "lab-fleet" in response.text
