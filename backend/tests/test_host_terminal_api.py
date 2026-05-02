import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings as process_settings
from app.main import app
from app.models.host import Host, HostStatus, OSType
from app.services.settings_service import settings_service


def _configure_terminal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    origins: str = "",
    token: str | None = None,
    auth_enabled: bool = False,
    agent_scheme: str = "ws",
) -> None:
    """Seed runtime settings + process env state for a terminal test."""
    monkeypatch.setitem(settings_service._cache, "agent.enable_web_terminal", enabled)
    monkeypatch.setitem(settings_service._cache, "agent.web_terminal_allowed_origins", origins)
    monkeypatch.setattr(process_settings, "agent_terminal_token", token)
    monkeypatch.setattr(process_settings, "agent_terminal_scheme", agent_scheme)
    monkeypatch.setattr(process_settings, "auth_enabled", auth_enabled)


def test_terminal_route_rejects_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch, setup_database: AsyncEngine
) -> None:
    _configure_terminal(monkeypatch, enabled=False)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/api/hosts/{uuid.uuid4()}/terminal"):
        pass


def test_terminal_route_rejects_when_host_not_found(
    monkeypatch: pytest.MonkeyPatch, setup_database: AsyncEngine
) -> None:
    _configure_terminal(
        monkeypatch,
        enabled=True,
        origins="http://testserver",
        token="tkn",
    )
    # Mock host_service.get_host to return None (host not found)
    with patch(
        "app.routers.host_terminal.host_service.get_host",
        new=AsyncMock(return_value=None),
    ):
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/hosts/{uuid.uuid4()}/terminal",
                headers={"origin": "http://testserver"},
            ),
        ):
            pass


def test_terminal_route_rejects_offline_host(monkeypatch: pytest.MonkeyPatch, setup_database: AsyncEngine) -> None:
    _configure_terminal(
        monkeypatch,
        enabled=True,
        origins="http://testserver",
        token="tkn",
    )
    # Build a fake offline host in memory — avoids schema-isolation issues.
    offline_host = Host(
        hostname="offline-host",
        ip="10.0.0.5",
        agent_port=5100,
        os_type=OSType.linux,
        status=HostStatus.offline,
    )
    offline_host.id = uuid.uuid4()

    with patch(
        "app.routers.host_terminal.host_service.get_host",
        new=AsyncMock(return_value=offline_host),
    ):
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/hosts/{offline_host.id}/terminal",
                headers={"origin": "http://testserver"},
            ),
        ):
            pass


def test_terminal_route_proxies_online_host_and_audits_session(
    monkeypatch: pytest.MonkeyPatch, setup_database: AsyncEngine
) -> None:
    _configure_terminal(
        monkeypatch,
        enabled=True,
        origins="http://testserver",
        token="tkn",
    )
    online_host = Host(
        hostname="online-host",
        ip="10.0.0.6",
        agent_port=5101,
        os_type=OSType.linux,
        status=HostStatus.online,
    )
    online_host.id = uuid.uuid4()
    session_id = uuid.uuid4()
    open_session = AsyncMock(return_value=session_id)
    close_session = AsyncMock()
    proxy_terminal = AsyncMock(return_value="client_disconnect")

    with (
        patch("app.routers.host_terminal.host_service.get_host", new=AsyncMock(return_value=online_host)),
        patch("app.routers.host_terminal.host_terminal_audit.open_session", new=open_session),
        patch("app.routers.host_terminal.host_terminal_audit.close_session", new=close_session),
        patch("app.routers.host_terminal.proxy_terminal_session", new=proxy_terminal),
    ):
        client = TestClient(app)
        with (
            client.websocket_connect(
                f"/api/hosts/{online_host.id}/terminal",
                headers={"origin": "http://testserver"},
            ) as ws,
            pytest.raises(WebSocketDisconnect),
        ):
            ws.receive_text()

    open_session.assert_awaited_once()
    assert open_session.await_args.kwargs["host_id"] == online_host.id
    assert open_session.await_args.kwargs["opened_by"] is None
    proxy_terminal.assert_awaited_once()
    assert proxy_terminal.await_args.kwargs["agent_url"] == "ws://10.0.0.6:5101/agent/terminal"
    assert proxy_terminal.await_args.kwargs["agent_token"] == "tkn"
    close_session.assert_awaited_once_with(
        mock_db := close_session.await_args.args[0],
        session_id=session_id,
        close_reason="client_disconnect",
    )
    assert mock_db is not None


def test_terminal_route_uses_configured_agent_websocket_scheme(
    monkeypatch: pytest.MonkeyPatch, setup_database: AsyncEngine
) -> None:
    _configure_terminal(
        monkeypatch,
        enabled=True,
        origins="http://testserver",
        token="tkn",
        agent_scheme="wss",
    )
    online_host = Host(
        hostname="online-host",
        ip="10.0.0.6",
        agent_port=5101,
        os_type=OSType.linux,
        status=HostStatus.online,
    )
    online_host.id = uuid.uuid4()
    open_session = AsyncMock(return_value=uuid.uuid4())
    close_session = AsyncMock()
    proxy_terminal = AsyncMock(return_value="client_disconnect")

    with (
        patch("app.routers.host_terminal.host_service.get_host", new=AsyncMock(return_value=online_host)),
        patch("app.routers.host_terminal.host_terminal_audit.open_session", new=open_session),
        patch("app.routers.host_terminal.host_terminal_audit.close_session", new=close_session),
        patch("app.routers.host_terminal.proxy_terminal_session", new=proxy_terminal),
    ):
        client = TestClient(app)
        with (
            client.websocket_connect(
                f"/api/hosts/{online_host.id}/terminal",
                headers={"origin": "http://testserver"},
            ) as ws,
            pytest.raises(WebSocketDisconnect),
        ):
            ws.receive_text()

    proxy_terminal.assert_awaited_once()
    assert proxy_terminal.await_args.kwargs["agent_url"] == "wss://10.0.0.6:5101/agent/terminal"
