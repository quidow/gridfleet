import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.schemas.portability import ExportBundle, ExportedDevice, OriginalHost
from app.devices.services.portability_import import validate_bundle
from tests.helpers import seed_existing_device, seed_host_named


def _bundle(devices: list[ExportedDevice]) -> ExportBundle:
    return ExportBundle(
        schema_version=1,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        devices=devices,
    )


def _device(
    *,
    hostname: str = "lab-04",
    host_id: uuid.UUID | None = None,
    identity_value: str = "R58",
    identity_scope: str = "host",
    identity_scheme: str = "android_serial",
) -> ExportedDevice:
    return ExportedDevice(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme=identity_scheme,
        identity_scope=identity_scope,  # type: ignore[arg-type]
        identity_value=identity_value,
        name="Pixel",
        device_type="real_device",
        connection_type="usb",
        original_host=OriginalHost(hostname=hostname, host_id=host_id),
    )


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_classifies_new_row_as_valid_new(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "lab-04")
    preview = await validate_bundle(db_session, _bundle([_device(hostname="lab-04", host_id=host.id)]))
    assert preview.rows[0].status.value == "valid_new"
    assert preview.rows[0].host_suggestion is not None
    assert preview.rows[0].host_suggestion.id == host.id


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_hostname_match_is_case_insensitive(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "Lab-Host-04")
    preview = await validate_bundle(db_session, _bundle([_device(hostname="lab-host-04")]))
    assert preview.rows[0].host_suggestion is not None
    assert preview.rows[0].host_suggestion.id == host.id


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_flags_intra_bundle_duplicates(db_session: AsyncSession) -> None:
    await seed_host_named(db_session, "lab-04")
    preview = await validate_bundle(db_session, _bundle([_device(identity_value="R58"), _device(identity_value="R58")]))
    assert {row.status.value for row in preview.rows} == {"duplicate_in_bundle"}


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_flags_existing_global_identity_as_conflict_skip(db_session: AsyncSession) -> None:
    await seed_host_named(db_session, "lab-04")
    await seed_existing_device(
        db_session,
        identity_scheme="udid",
        identity_value="GLOBAL-1",
        identity_scope="global",
    )
    preview = await validate_bundle(
        db_session,
        _bundle([_device(identity_value="GLOBAL-1", identity_scope="global", identity_scheme="udid")]),
    )
    assert preview.rows[0].status.value == "conflict_skip"


@pytest.mark.asyncio
@pytest.mark.db
async def test_validate_returns_bundle_hash_and_available_hosts(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "lab-04")
    preview = await validate_bundle(db_session, _bundle([_device(hostname="lab-04", host_id=host.id)]))
    assert preview.bundle_hash.startswith("sha256:")
    assert any(h.id == host.id for h in preview.available_hosts)
