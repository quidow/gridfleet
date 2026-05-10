from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_app.grid_node.protocol import Slot


@dataclass(frozen=True)
class GridNodeConfig:
    node_id: str
    node_uri: str
    appium_upstream: str
    slots: list[Slot]
    hub_publish_url: str
    hub_subscribe_url: str
    heartbeat_sec: float
    session_timeout_sec: float
    proxy_timeout_sec: float
