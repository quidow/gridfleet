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
    # Local bind host for the uvicorn grid-node HTTP server. Defaults to
    # `0.0.0.0` so the server binds every local interface; the advertised
    # `node_uri` (which carries `AGENT_ADVERTISE_IP`) is independent and may
    # point at a hostname that does not resolve locally — e.g.
    # `host.docker.internal` from inside a docker-compose hub container.
    bind_host: str = "0.0.0.0"
