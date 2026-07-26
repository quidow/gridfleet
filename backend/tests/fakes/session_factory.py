"""A ``session_factory`` stand-in for unit tests that call routes directly.

Routes that own a command boundary open ``session_factory.begin()`` (or
``session_factory()`` for a short read). Unit tests that call the route function
with mock services need something that satisfies both shapes without a database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class FakeSessionFactory:
    """Hands out one caller-supplied session object for every context it opens."""

    def __init__(self, session: object = None) -> None:
        self.session = session
        self.begun = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        self.begun += 1
        yield self.session

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[object]:
        yield self.session
