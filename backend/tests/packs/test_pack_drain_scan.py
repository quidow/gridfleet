from typing import TYPE_CHECKING

import pytest

from app.packs.models import DriverPack, PackState
from app.packs.services.lifecycle import PackLifecycleService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_complete_draining_packs_once_disables_empty_draining_pack(db_session: AsyncSession) -> None:
    pack = DriverPack(
        id="draining-pack",
        display_name="Draining Pack",
        maintainer="tests",
        license="Apache-2.0",
        state=PackState.draining,
    )
    db_session.add(pack)
    await db_session.commit()

    changed = await PackLifecycleService().complete_draining_packs_once(db_session)

    assert changed == ["draining-pack"]
    refreshed = await db_session.get(DriverPack, "draining-pack")
    assert refreshed is not None
    assert refreshed.state == PackState.disabled
