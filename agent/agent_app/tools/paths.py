"""PATH-agnostic host tool resolution helpers."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _parse_node_version(path: str) -> tuple[int, ...]:
    """Extract a sortable version tuple from an nvm node path like .../v24.12.0/bin/appium."""
    try:
        parts = path.split(os.sep)
        for part in parts:
            if part.startswith("v") and "." in part:
                return tuple(int(x) for x in part.lstrip("v").split("."))
    except ValueError, IndexError:
        logger.debug("Failed to parse node version from %r", path, exc_info=True)
    return (0,)
