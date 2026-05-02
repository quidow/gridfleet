from __future__ import annotations

from typing import TYPE_CHECKING

from app.services import control_plane_state_store

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_patch_value_inserts_when_missing(db_session: AsyncSession) -> None:
    await control_plane_state_store.patch_value(
        db_session,
        "test.patch",
        "device-1",
        {"node_running": True},
    )
    await db_session.commit()

    value = await control_plane_state_store.get_value(db_session, "test.patch", "device-1")

    assert value == {"node_running": True}


async def test_patch_value_merges_top_level_object(db_session: AsyncSession) -> None:
    await control_plane_state_store.set_value(
        db_session,
        "test.patch",
        "device-2",
        {"device_checks_healthy": False, "last_checked_at": "first"},
    )
    await db_session.commit()

    await control_plane_state_store.patch_value(
        db_session,
        "test.patch",
        "device-2",
        {"node_running": True, "last_checked_at": "second"},
    )
    await db_session.commit()

    value = await control_plane_state_store.get_value(db_session, "test.patch", "device-2")

    assert value == {
        "device_checks_healthy": False,
        "node_running": True,
        "last_checked_at": "second",
    }
