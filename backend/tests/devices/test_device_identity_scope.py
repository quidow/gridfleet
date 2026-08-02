from __future__ import annotations

from unittest.mock import AsyncMock

from app.devices.services.identity import is_host_scoped_identity
from app.devices.services.identity_conflicts import DeviceIdentityConflictService


def test_host_scoped() -> None:
    assert is_host_scoped_identity(identity_scope="host") is True


def test_global_scoped() -> None:
    assert is_host_scoped_identity(identity_scope="global") is False


def test_none_scoped() -> None:
    assert is_host_scoped_identity(identity_scope=None) is False


async def test_find_device_identity_conflict_returns_none_without_an_identity_scheme() -> None:
    result = await DeviceIdentityConflictService().find_device_identity_conflict(
        AsyncMock(),
        identity_scope="global",
        identity_scheme=None,
        identity_value="serial",
        host_id=None,
    )
    assert result is None


async def test_find_device_identity_conflict_returns_none_for_host_scoped_identity_without_a_host() -> None:
    result = await DeviceIdentityConflictService().find_device_identity_conflict(
        AsyncMock(),
        identity_scope="host",
        identity_scheme="serial",
        identity_value="serial",
        host_id=None,
    )
    assert result is None
