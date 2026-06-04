"""Typed sentinel for 'argument not provided' where None is a meaningful value."""

from __future__ import annotations

from typing import Final


class UnsetType:
    """Singleton sentinel distinguishing 'do not write' from an explicit None."""

    _instance: UnsetType | None = None

    def __new__(cls) -> UnsetType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = UnsetType()
