"""Verify DI dependencies resolve correctly."""

from __future__ import annotations

import inspect

from app.dependencies import get_app_services


def test_get_app_services_signature() -> None:
    """get_app_services accepts a Request parameter."""
    sig = inspect.signature(get_app_services)
    assert "request" in sig.parameters


def test_get_app_services_extracts_from_state() -> None:
    """get_app_services returns request.app.state.services."""
    from unittest.mock import MagicMock

    mock_request = MagicMock()
    mock_request.app.state.services = "sentinel"
    result = get_app_services(mock_request)
    assert result == "sentinel"
