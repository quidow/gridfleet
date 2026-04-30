"""Shared state threaded into every factory call for determinism."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class SeedContext:
    """Bundle of session + RNG + frozen-now reference passed through factories."""

    session: AsyncSession
    rng: random.Random
    now: datetime

    @classmethod
    def build(cls, *, session: AsyncSession, seed: int, now: datetime | None = None) -> SeedContext:
        frozen_now = (now or datetime.now(UTC)).astimezone(UTC)
        return cls(session=session, rng=random.Random(seed), now=frozen_now)
