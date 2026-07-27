"""``session_factory`` stand-ins for tests that drive a command boundary directly.

Routes and services that own a command boundary open ``session_factory.begin()``
(or ``session_factory()`` for a short read). Two shapes are needed:

* :class:`FakeSessionFactory` for unit tests that call a route function with mock
  services and no database at all; and
* :class:`RecordingSessionFactory` for tests that need real sessions on the real
  test schema but also need to see, and interfere with, every session a command
  opened.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type ExecuteHook = Callable[[AsyncSession, str], Awaitable[None]]
type StatementPinner = Callable[[AsyncSession, list[str]], Callable[[], None]]


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


class RecordingSessionFactory:
    """An ``async_sessionmaker`` stand-in that keeps every session it hands out.

    Supports both shapes a Phase 9 command uses — ``factory()`` for a short read
    and ``factory.begin()`` for the single write boundary — over real sessions on
    the real test schema. A failure a test injects through ``hook`` is therefore a
    real database failure on a real transaction, not a patched method leaving a
    clean session behind.

    ``hook`` runs after each ``session.execute`` with the statement lowercased and
    whitespace-collapsed, which is what lets a test commit a racing peer, or park
    a coroutine, at an exact point in a command's statement sequence. It hangs off
    ``session.execute`` rather than the engine's cursor events because a hook has
    to be able to await, which a ``before_cursor_execute`` listener cannot.

    *statement_pinner* is a parameter rather than a direct import so this module
    stays free of ``tests.concurrency.group_lock_helpers``: that module imports
    ``tests.helpers``, which imports ``tests.fakes``, so importing it here would
    make ``tests.fakes`` import itself while still half-initialised. Pass
    ``pin_statement_listener`` when a test reads ``statements_for`` — an ORM flush
    issues its UPDATE on the connection, never through ``session.execute``, so the
    ``hook`` wrapper alone cannot see the writes. A caller that pins must call
    ``close()``; without a pinner ``close()`` is a no-op.
    """

    def __init__(
        self,
        inner: async_sessionmaker[AsyncSession],
        *,
        statement_pinner: StatementPinner | None = None,
    ) -> None:
        self._inner = inner
        self._statement_pinner = statement_pinner
        self._detach: list[Callable[[], None]] = []
        self.sessions: list[AsyncSession] = []
        self.statements: list[list[str]] = []
        self.begun = 0
        self.hook: ExecuteHook | None = None

    def _track(self, session: AsyncSession) -> AsyncSession:
        self.sessions.append(session)
        sink: list[str] = []
        self.statements.append(sink)
        if self._statement_pinner is not None:
            self._detach.append(self._statement_pinner(session, sink))
        original = session.execute

        async def spy(statement: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            result = await original(statement, *args, **kwargs)
            if self.hook is not None:
                await self.hook(session, " ".join(str(statement).lower().split()))
            return result

        session.execute = spy  # type: ignore[method-assign]
        return session

    def __call__(self) -> AsyncSession:
        return self._track(self._inner())

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._inner() as session:
            self._track(session)
            self.begun += 1
            async with session.begin():
                yield session

    def close(self) -> None:
        for detach in self._detach:
            detach()
        self._detach.clear()

    def open_transactions(self) -> list[int]:
        """Indexes of recorded sessions still inside a transaction, right now."""
        return [index for index, session in enumerate(self.sessions) if session.in_transaction()]

    def statements_for(self, index: int) -> list[str]:
        return [" ".join(statement.lower().split()) for statement in self.statements[index]]
