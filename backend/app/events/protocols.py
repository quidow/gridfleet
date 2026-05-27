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


@runtime_checkable
class EventSubscriber(Protocol):
    def subscribe(self) -> asyncio.Queue[Any]: ...
    def unsubscribe(self, q: asyncio.Queue[Any]) -> None: ...
