from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol

from app.models.device import Device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.appium_node import AppiumNode

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type SettingValue = Any
type ControlPlaneValue = Any
type AsyncTaskFactory = Callable[..., Coroutine[object, object, None]]
type ProbeSessionFn = Callable[[JsonObject, int], Awaitable[tuple[bool, str | None]]]


class AsyncSessionContextManager(Protocol):
    async def __aenter__(self) -> AsyncSession: ...

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool | None: ...


class SessionFactory(Protocol):
    def __call__(self) -> AsyncSessionContextManager: ...


class NodeStopper(Protocol):
    async def stop_node(self, db: AsyncSession, device: Device) -> AppiumNode: ...


type NodeManagerResolver = Callable[[Device], NodeStopper]
