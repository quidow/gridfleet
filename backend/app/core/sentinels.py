"""Typed sentinel for 'argument not provided' where None is a meaningful value."""

from __future__ import annotations

from typing import Final


class UnsetType:
    """Sentinel distinguishing 'do not write' from an explicit None."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = UnsetType()
