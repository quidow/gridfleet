"""Group-command boundary regressions for the Phase 11 conversion.

Each test pins a property the router-owned boundary must keep: nothing partial
survives a mid-command failure, and the dynamic count still degrades to null
instead of failing the whole command.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from app.devices.models import DeviceGroup, DeviceGroupMemberOf
from app.devices.services import groups as groups_module

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _make_static(client: AsyncClient, key: str) -> None:
    response = await client.post(
        "/api/device-groups",
        json={"key": key, "name": key, "group_type": "static"},
    )
    assert response.status_code == 201, response.text


async def test_create_group_failure_leaves_no_row_and_no_edge(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise after the group INSERT must take the whole definition down.

    The insert flushes before the reference rows are written, so an exception
    between the two is exactly the interleaving that would otherwise commit a
    group with a half-written reference set.
    """
    static_key = f"phase11-target-{uuid.uuid4().hex[:8]}"
    dynamic_key = f"phase11-source-{uuid.uuid4().hex[:8]}"
    await _make_static(client, static_key)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected mid-command failure")

    monkeypatch.setattr(groups_module, "_replace_member_of", _boom)

    with pytest.raises(RuntimeError, match="injected mid-command failure"):
        await client.post(
            "/api/device-groups",
            json={
                "key": dynamic_key,
                "name": dynamic_key,
                "group_type": "dynamic",
                "filters": {"member_of": [static_key]},
            },
        )

    db_session.expire_all()
    surviving = await db_session.scalar(
        select(func.count()).select_from(DeviceGroup).where(DeviceGroup.key == dynamic_key)
    )
    assert surviving == 0, "a failed create_group left the group row behind"
    edges = await db_session.scalar(select(func.count()).select_from(DeviceGroupMemberOf))
    assert edges == 0, "a failed create_group left member_of edges behind"


async def test_create_dynamic_group_reports_null_count_when_the_count_read_fails(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nullable dynamic count is public API: a failed count must not fail the write."""
    dynamic_key = f"phase11-nullcount-{uuid.uuid4().hex[:8]}"

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected count failure")

    monkeypatch.setattr(groups_module, "load_member_of_keys", _boom)

    response = await client.post(
        "/api/device-groups",
        json={"key": dynamic_key, "name": dynamic_key, "group_type": "dynamic", "filters": {"status": "available"}},
    )

    assert response.status_code == 201, response.text
    assert "device_count" not in response.json(), "a null count must be omitted, not reported as a number"

    # A direct read, not the list endpoint: ``list_groups`` calls the same
    # patched ``load_member_of_keys`` with no error handling, so routing this
    # check through GET /api/device-groups would crash on an unrelated,
    # untouched code path instead of verifying the create's own durability.
    db_session.expire_all()
    surviving = await db_session.scalar(
        select(func.count()).select_from(DeviceGroup).where(DeviceGroup.key == dynamic_key)
    )
    assert surviving == 1, "the group must have been committed anyway"
