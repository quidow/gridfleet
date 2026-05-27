"""Event-domain Protocol definitions.

These are the contracts that consumers of the event system depend on.
The EventBus class satisfies all of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import asyncio

    from app.events.catalog import EventSeverity


@runtime_checkable
class EventPublisher(Protocol):
    async def publish(
        self, event_type: str, data: dict[str, Any], *, severity: EventSeverity | None = None
    ) -> None: ...

    def track_task(self, task: asyncio.Task[None]) -> None: ...


@runtime_checkable
class EventSubscriber(Protocol):
    def subscribe(self) -> asyncio.Queue[Any]: ...
    def unsubscribe(self, q: asyncio.Queue[Any]) -> None: ...


@runtime_checkable
class EventReader(Protocol):
    async def get_recent_events_persisted(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        event_types: list[str] | None = None,
        severities: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...
