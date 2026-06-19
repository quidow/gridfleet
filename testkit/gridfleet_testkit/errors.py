"""Typed errors raised by the GridFleet client."""

from __future__ import annotations


class UnknownIncludeError(ValueError):
    """Backend rejected one or more `?include=` keys."""

    def __init__(self, values: list[str]) -> None:
        super().__init__(f"Backend rejected unknown include values: {values}")
        self.values = values


class ReserveCapabilitiesUnsupportedError(ValueError):
    """`?include=capabilities` is not supported on reserve."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "include=capabilities is not supported on reserve")
