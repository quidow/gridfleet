from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GridConfig(BaseSettings):
    """Per-domain process config for Selenium Grid integration.

    Holds connection details for the hub's ZMQ event bus. The hub URL
    itself stays in the DB settings registry (``grid.hub_url``) because
    operators routinely reconfigure it from the Settings UI; bus URLs
    are infra-level and only change at deploy time.
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
