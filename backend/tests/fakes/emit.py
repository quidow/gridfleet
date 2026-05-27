"""Fake event emitter for tests."""

from __future__ import annotations

from typing import Any


class FakeEmit:
    """Captures emitted events for assertions. Satisfies EmitProtocol."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event_type: str, payload: dict[str, Any]) -> None:
        self.emitted.append((event_type, payload))

    def reset(self) -> None:
        self.emitted.clear()

    def last(self) -> tuple[str, dict[str, Any]]:
        return self.emitted[-1]

    def types(self) -> list[str]:
        return [t for t, _ in self.emitted]
