"""Foundation checks for the two-axis health-guard revision source."""

from typing import TYPE_CHECKING

from app.core.observation_revision import next_observation_revision

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_next_observation_revision_is_strictly_monotonic(db_session: AsyncSession) -> None:
    first = await next_observation_revision(db_session)
    second = await next_observation_revision(db_session)
    third = await next_observation_revision(db_session)
    assert first < second < third
