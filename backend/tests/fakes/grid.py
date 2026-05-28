"""Shared fake for ``GridServiceProtocol`` used in unit tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

from app.grid.service import GridService


def make_fake_grid(
    grid_data: dict[str, Any] | None = None,
    *,
    terminate_result: bool = True,
) -> AsyncMock:
    """Return an ``AsyncMock`` that conforms to ``GridServiceProtocol``.

    Defaults:
    - ``get_status()`` returns ``grid_data`` (or ``{}`` when not provided),
      which makes ``available_node_device_ids`` return ``None`` — i.e. "Grid
      status unavailable, do not fence". Pass ``{"value": {"ready": True,
      "nodes": []}}`` to model a healthy-but-empty Grid.
    - ``terminate_session()`` returns ``terminate_result`` (default ``True``).
    - ``available_node_device_ids`` dispatches to the real (pure) parser on
      ``GridService``, so tests get realistic node-id extraction without
      having to stub it.

    Tests can override any method on the returned mock after construction.
    """
    fake = AsyncMock()
    fake.get_status = AsyncMock(return_value=grid_data if grid_data is not None else {})
    fake.terminate_session = AsyncMock(return_value=terminate_result)
    fake.available_node_device_ids = Mock(side_effect=GridService.available_node_device_ids)
    return fake
