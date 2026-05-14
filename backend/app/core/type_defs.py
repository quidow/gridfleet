from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type SettingValue = Any
type ControlPlaneValue = Any
type AsyncTaskFactory = Callable[..., Coroutine[object, object, None]]


class ProbeSessionFn(Protocol):
    def __call__(
        self,
        capabilities: JsonObject,
        timeout_sec: int,
        *,
        grid_url: str | None = None,
    ) -> Awaitable[tuple[bool, str | None]]:
        raise NotImplementedError


class AsyncSessionContextManager(Protocol):
    async def __aenter__(self) -> AsyncSession:
        raise NotImplementedError

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool | None:
        raise NotImplementedError


class SessionFactory(Protocol):
    def __call__(self) -> AsyncSessionContextManager:
        raise NotImplementedError
