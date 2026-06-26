"""Event-domain Protocol definitions.

These are the contracts that consumers of the event system depend on.
The EventBus class satisfies all of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

    from app.events.catalog import EventSeverity


class EventPublisher(Protocol):
    async def publish(
        self, event_type: str, data: dict[str, Any], *, severity: EventSeverity | None = None
    ) -> None: ...

    def track_task(self, task: asyncio.Task[None]) -> None: ...

    def queue_for_session(
        self,
        db: AsyncSession | Session,
        event_type: str,
        data: dict[str, Any],
        *,
        severity: EventSeverity | None = None,
    ) -> None: ...
