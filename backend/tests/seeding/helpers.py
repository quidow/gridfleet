from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.seeding.context import SeedContext


def no_persistence_session() -> AsyncSession:
    """Return the deliberate non-persistent test sentinel typed as AsyncSession."""
    return cast("AsyncSession", None)


def build_test_seed_context(*, seed: int) -> SeedContext:
    return SeedContext.build(session=no_persistence_session(), seed=seed)
