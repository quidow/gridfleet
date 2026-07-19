"""Breaking-contract regressions: the backend has no device-tag feature.

Tags were migrated to static device groups. Nothing in the model, schemas,
filters, bulk API, run requirements, readiness, verification, or portability
may accept or emit them.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from app.devices.models import Device
from app.devices.schemas import device as device_schemas
from app.devices.schemas.device import DevicePatch, DeviceRead, DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.schemas.filters import DeviceGroupFilters, DeviceQueryFilters
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.readiness import READINESS_IMPACTING_FIELDS
from app.portability.schemas import InventoryColumn
from app.runs.schemas import DeviceRequirement, ReservedDeviceInfo

if TYPE_CHECKING:
    from httpx2 import AsyncClient


def test_device_model_has_no_tags_column() -> None:
    assert "tags" not in Device.__table__.columns
    assert "ix_devices_tags_gin" not in {index.name for index in Device.__table__.indexes}


def test_device_read_and_filters_drop_tags() -> None:
    assert "tags" not in DeviceRead.model_fields
    assert "tags" not in DeviceGroupFilters.model_fields
    assert "tags" not in DeviceQueryFilters.model_fields


@pytest.mark.parametrize("model", [DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate])
def test_device_write_schemas_reject_tags(model: type[DevicePatch]) -> None:
    assert "tags" not in model.model_fields
    with pytest.raises(ValidationError):
        model.model_validate({"host_id": uuid.uuid4(), "tags": {"team": "qa"}})


def test_bulk_tag_schema_and_service_are_gone() -> None:
    assert not hasattr(device_schemas, "BulkTagsUpdate")
    assert not hasattr(device_schemas, "DeviceTags")
    assert not hasattr(BulkOperationsService, "bulk_update_tags")


def test_run_requirement_and_reserved_device_drop_tags() -> None:
    assert "tags" not in DeviceRequirement.model_fields
    assert "tags" not in ReservedDeviceInfo.model_fields


def test_readiness_and_portability_drop_tags() -> None:
    assert "tags" not in READINESS_IMPACTING_FIELDS
    assert "tags" not in {column.value for column in InventoryColumn}


async def test_bulk_update_tags_endpoints_are_removed(client: AsyncClient) -> None:
    body = {"device_ids": [str(uuid.uuid4())], "tags": {"team": "qa"}, "merge": True}
    assert (await client.post("/api/devices/bulk/update-tags", json=body)).status_code == 404
    assert (await client.post("/api/device-groups/any-key/bulk/update-tags", json=body)).status_code == 404


async def test_tag_query_params_do_not_filter_devices(client: AsyncClient) -> None:
    unfiltered = await client.get("/api/devices")
    filtered = await client.get("/api/devices", params={"tags.team": "qa"})
    assert filtered.status_code == 200
    assert filtered.json() == unfiltered.json()
