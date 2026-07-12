"""Unit tests for pure-dict lifecycle_policy_state helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from app.devices.services.lifecycle_policy_state import default_state, in_maintenance, state, write_state

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host

from tests.helpers import create_device


class _DeviceStub:
    def __init__(self, lifecycle_policy_state: dict[str, object] | None) -> None:
        self.lifecycle_policy_state = lifecycle_policy_state


def test_in_maintenance_reads_reason_with_defaults_merged() -> None:
    assert in_maintenance(cast("Device", _DeviceStub({"maintenance_reason": "operator"}))) is True
    assert in_maintenance(cast("Device", _DeviceStub({"maintenance_reason": None}))) is False
    assert in_maintenance(cast("Device", _DeviceStub({}))) is False
    assert in_maintenance(cast("Device", _DeviceStub(None))) is False


def test_default_state_contains_only_maintenance_reason() -> None:
    assert default_state() == {"maintenance_reason": None}


def test_state_discards_retired_policy_keys() -> None:
    device = _DeviceStub(
        {
            "maintenance_reason": "operator",
            "deferred_stop": True,
            "deferred_stop_reason": "retired",
        }
    )

    assert state(cast("Device", device)) == {"maintenance_reason": "operator"}


@pytest.mark.db
async def test_write_state_filters_retired_ladder_keys(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="state-filter")
    write_state(
        device,
        {
            "maintenance_reason": "operator",
            "deferred_stop": True,
            "deferred_stop_reason": "busy",
            "deferred_stop_since": "2026-07-12T12:00:00+00:00",
            "last_action": "retired",
            "backoff_until": "retired",
        },
    )

    assert device.lifecycle_policy_state == {"maintenance_reason": "operator"}
