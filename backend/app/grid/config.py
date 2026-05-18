from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GridConfig(BaseSettings):
    """Per-domain process config for Selenium Grid integration.

    Holds connection details for the hub's ZMQ event bus. The hub URL
    itself stays in the DB settings registry (``grid.hub_url``) because
    operators routinely reconfigure it from the Settings UI; bus URLs
    are infra-level and only change at deploy time.

    Bus port assignments (Selenium Grid 4 defaults):
      * 4442 — hub XPUB. Backend connects its ZMQ SUB socket here to read events.
      * 4443 — hub XSUB. Backend connects a ZMQ PUB socket here only if it ever
        needs to inject events into the bus (currently never).

    ``event_bus_subscribe_url`` MUST point at the XPUB port (default 4442).
    ``event_bus_publish_url`` MUST point at the XSUB port (default 4443).
    See the agent twin ``agent_app/grid_node/event_bus.py`` for the matching
    wire format and Selenium ``UnboundZmqEventBus`` for the reference impl.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        populate_by_name=True,
        extra="ignore",
    )

    event_bus_subscribe_url: str = Field(
        default="tcp://selenium-hub:4442",
        alias="GRIDFLEET_GRID_EVENT_BUS_SUBSCRIBE_URL",
    )
    event_bus_publish_url: str = Field(
        default="tcp://selenium-hub:4443",
        alias="GRIDFLEET_GRID_EVENT_BUS_PUBLISH_URL",
    )
