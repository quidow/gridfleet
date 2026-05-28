"""Verify event domain dependency wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.events.dependencies import get_event_services


def test_get_event_services_extracts_from_state() -> None:
    mock_request = MagicMock()
    mock_services = MagicMock()
    mock_request.app.state.services.events = mock_services
    result = get_event_services(mock_request)
    assert result is mock_services
