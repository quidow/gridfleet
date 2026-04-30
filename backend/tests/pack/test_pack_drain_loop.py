import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.driver_pack import DriverPack, PackState
from app.services.pack_drain import complete_draining_packs_once

pytestmark = pytest.mark.asyncio


async def test_complete_draining_packs_once_disables_empty_draining_pack(db_session: AsyncSession) -> None:
    pack = DriverPack(
        id="draining-pack",
        origin="uploaded",
        display_name="Draining Pack",
        maintainer="tests",
        license="Apache-2.0",
        state=PackState.draining,
    )
    db_session.add(pack)
    await db_session.commit()

    changed = await complete_draining_packs_once(db_session)

    assert changed == ["draining-pack"]
    refreshed = await db_session.get(DriverPack, "draining-pack")
    assert refreshed is not None
    assert refreshed.state == PackState.disabled
