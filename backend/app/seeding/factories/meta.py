"""Meta factories — DeviceGroup, Setting."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.devices.models import DeviceGroup, GroupType
from app.settings.models import Setting

if TYPE_CHECKING:
    from app.seeding.context import SeedContext


def make_device_group(
    ctx: SeedContext,
    *,
    name: str,
    group_type: GroupType = GroupType.static,
    description: str | None = None,
    filters: dict[str, Any] | None = None,
) -> DeviceGroup:
    """Build an unflushed DeviceGroup with the given parameters."""
    del ctx  # unused; present for uniform factory signature
    return DeviceGroup(
        name=name,
        group_type=group_type,
        description=description,
        filters=filters,
    )


def make_setting(
    ctx: SeedContext,
    *,
    key: str,
    value: int | float | str | bool | dict[str, object] | list[object] | None,
    category: str,
) -> Setting:
    """Build an unflushed Setting with the given parameters."""
    del ctx  # unused; present for uniform factory signature
    return Setting(
        key=key,
        value=value,
        category=category,
    )
