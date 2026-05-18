"""Per-domain GridConfig BaseSettings — bus URL fields.

Mirrors the per-domain pattern documented in CLAUDE.md (auth, agent,
packs): env_prefix unset, populate_by_name=True, Field aliases preserve
GRIDFLEET_* env-var names.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from app.grid.config import GridConfig


@pytest.fixture(autouse=True)
def _clear_bus_env() -> Iterator[None]:
    keys = ("GRIDFLEET_GRID_EVENT_BUS_SUBSCRIBE_URL", "GRIDFLEET_GRID_EVENT_BUS_PUBLISH_URL")
    saved = {key: os.environ.pop(key, None) for key in keys}
    yield
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value


def test_grid_config_defaults_to_compose_hub() -> None:
    cfg = GridConfig()
    assert cfg.event_bus_subscribe_url == "tcp://selenium-hub:4442"
    assert cfg.event_bus_publish_url == "tcp://selenium-hub:4443"


def test_grid_config_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_GRID_EVENT_BUS_SUBSCRIBE_URL", "tcp://hub.example:4442")
    monkeypatch.setenv("GRIDFLEET_GRID_EVENT_BUS_PUBLISH_URL", "tcp://hub.example:4443")
    cfg = GridConfig()
    assert cfg.event_bus_subscribe_url == "tcp://hub.example:4442"
    assert cfg.event_bus_publish_url == "tcp://hub.example:4443"


def test_grid_config_constructs_by_field_name() -> None:
    """populate_by_name=True is mandatory; tests construct via field-name kwargs."""
    cfg = GridConfig(
        event_bus_subscribe_url="tcp://x:1",
        event_bus_publish_url="tcp://x:2",
    )
    assert cfg.event_bus_subscribe_url == "tcp://x:1"
    assert cfg.event_bus_publish_url == "tcp://x:2"
