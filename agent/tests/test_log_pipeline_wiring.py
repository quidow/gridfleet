from __future__ import annotations

import logging

from agent_app.logs.handlers import RingBufferHandler, ShipperHandler
from agent_app.observability import configure_logging


def test_configure_attaches_ring_buffer_and_shipper() -> None:
    configure_logging(force=True)
    root = logging.getLogger()
    assert any(isinstance(handler, RingBufferHandler) for handler in root.handlers)
    assert any(isinstance(handler, ShipperHandler) for handler in root.handlers)
