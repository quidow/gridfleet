"""Breaking-contract regressions: the backend has no device-tag feature.

Tags were migrated to static device groups. Nothing in the model, schemas,
filters, bulk API, run requirements, readiness, verification, or portability
may accept or emit them.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ValidationError

from app.devices.models import Device
from app.devices.schemas import device as device_schemas
from app.devices.schemas.device import DevicePatch, DeviceRead, DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.schemas.filters import DeviceGroupFilters, DeviceQueryFilters
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.readiness import READINESS_IMPACTING_FIELDS
from app.portability.schemas import ExportedDevice
from app.runs.schemas import DeviceRequirement, ReservedDeviceInfo
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def test_device_model_has_no_tags_column() -> None:
    assert "tags" not in Device.__table__.columns
    assert "ix_devices_tags_gin" not in {index.name for index in Device.__table__.indexes}


def test_device_read_and_filters_drop_tags() -> None:
    assert "tags" not in DeviceRead.model_fields
    assert "tags" not in DeviceGroupFilters.model_fields
    assert "tags" not in DeviceQueryFilters.model_fields


_HOST_ID = uuid.uuid4()

# Every payload is valid for its model except for the ``tags`` key the test adds,
# so the ValidationError is attributable to ``tags`` and not to a missing field.
_VALID_WRITE_PAYLOADS: list[tuple[type[BaseModel], dict[str, object]]] = [
    (DevicePatch, {"name": "device-a"}),
    (
        DeviceVerificationCreate,
        {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "name": "device-a", "host_id": _HOST_ID},
    ),
    (DeviceVerificationUpdate, {"host_id": _HOST_ID}),
]


@pytest.mark.parametrize(("model", "payload"), _VALID_WRITE_PAYLOADS)
def test_device_write_schemas_reject_tags(model: type[BaseModel], payload: dict[str, object]) -> None:
    assert "tags" not in model.model_fields
    model.model_validate(payload)  # The payload alone is valid — only ``tags`` may break it.
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "tags": {"team": "qa"}})


def test_bulk_tag_schema_and_service_are_gone() -> None:
    assert not hasattr(device_schemas, "BulkTagsUpdate")
    assert not hasattr(device_schemas, "DeviceTags")
    assert not hasattr(BulkOperationsService, "bulk_update_tags")


def test_run_requirement_and_reserved_device_drop_tags() -> None:
    assert "tags" not in DeviceRequirement.model_fields
    assert "tags" not in ReservedDeviceInfo.model_fields


def test_readiness_and_portability_drop_tags() -> None:
    assert "tags" not in READINESS_IMPACTING_FIELDS
    assert "tags" not in ExportedDevice.model_fields


async def test_bulk_update_tags_endpoints_are_removed(client: AsyncClient) -> None:
    body = {"device_ids": [str(uuid.uuid4())], "tags": {"team": "qa"}, "merge": True}
    assert (await client.post("/api/devices/bulk/update-tags", json=body)).status_code == 404
    assert (await client.post("/api/device-groups/any-key/bulk/update-tags", json=body)).status_code == 404


async def test_tag_query_params_do_not_filter_devices(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """An unknown ``tags.*`` param is ignored, not honoured as a filter."""
    for index in range(2):
        await create_device_record(
            db_session,
            host_id=default_host_id,
            identity_value=f"tagless-{index}",
            name=f"tagless-{index}",
        )

    unfiltered = await client.get("/api/devices")
    filtered = await client.get("/api/devices", params={"tags.team": "qa"})
    assert filtered.status_code == 200
    assert len(unfiltered.json()) == 2
    assert len(filtered.json()) == 2
    assert filtered.json() == unfiltered.json()
