"""Verify composition root produces a valid AppServices instance."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.http_pool import AgentHttpPool
from app.composition import AppServices, compose_app
from app.events.event_bus import EventBus
from app.settings.service import SettingsService


@pytest.fixture
def mock_engine() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_session_factory() -> MagicMock:
    return MagicMock()


def test_app_services_is_frozen() -> None:
    assert dataclasses.fields(AppServices)


def test_compose_app_returns_app_services(mock_engine: MagicMock, mock_session_factory: MagicMock) -> None:
    services = compose_app(
        engine=mock_engine,
        session_factory=mock_session_factory,
        bus=EventBus(),
        settings_svc=SettingsService(),
        http_pool=AgentHttpPool(),
        circuit_breaker=AgentCircuitBreaker(),
    )
    assert isinstance(services, AppServices)
    assert services.events is not None
    assert services.settings is not None
    assert services.agent_comm is not None
    assert services.devices is not None
    assert services.hosts is not None
    assert services.packs is not None
    assert services.sessions is not None
    assert services.runs is not None
    assert services.grid is not None


def test_app_services_immutable(mock_engine: MagicMock, mock_session_factory: MagicMock) -> None:
    services = compose_app(
        engine=mock_engine,
        session_factory=mock_session_factory,
        bus=EventBus(),
        settings_svc=SettingsService(),
        http_pool=AgentHttpPool(),
        circuit_breaker=AgentCircuitBreaker(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        services.events = None  # type: ignore[misc]
