import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from agent_app.config import ManagerSettings
from agent_app.host.version_guidance import clear_version_guidance, get_version_guidance
from agent_app.registration import _map_os_type, get_local_ip, register_with_manager, registration_loop


def test_get_local_ip_prefers_advertised_ip() -> None:
    with patch("agent_app.registration.agent_settings.core.advertise_ip", "10.0.0.9"):
        assert get_local_ip() == "10.0.0.9"


def test_get_local_ip_uses_udp_socket_address() -> None:
    socket_obj = MagicMock()
    socket_obj.getsockname.return_value = ("10.0.0.10", 54321)

    with (
        patch("agent_app.grid_url.agent_settings.core.advertise_ip", None),
        patch("agent_app.grid_url.socket.socket", return_value=socket_obj),
    ):
        assert get_local_ip() == "10.0.0.10"

    socket_obj.connect.assert_called_once_with(("8.8.8.8", 80))
    socket_obj.close.assert_called_once()


def test_get_local_ip_falls_back_to_hostname_lookup() -> None:
    with (
        patch("agent_app.grid_url.agent_settings.core.advertise_ip", None),
        patch("agent_app.grid_url.socket.socket", side_effect=OSError),
        patch("agent_app.grid_url.socket.gethostname", return_value="agent-host"),
        patch("agent_app.grid_url.socket.gethostbyname", return_value="127.0.0.1"),
    ):
        assert get_local_ip() == "127.0.0.1"


def test_map_os_type_maps_darwin_to_macos() -> None:
    with patch("agent_app.registration.platform.system", return_value="Darwin"):
        assert _map_os_type() == "macos"


def test_map_os_type_defaults_to_linux() -> None:
    with patch("agent_app.registration.platform.system", return_value="Linux"):
        assert _map_os_type() == "linux"


async def test_register_with_manager_sends_expected_payload() -> None:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    response = MagicMock(spec=httpx.Response)
    response.json.return_value = {"id": "host-1", "status": "online"}
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)

    with (
        patch(
            "agent_app.registration.get_or_refresh_capabilities_snapshot",
            new_callable=AsyncMock,
            return_value={"platforms": ["android_mobile"], "tools": {"adb": "1.0.41"}},
        ),
        patch("agent_app.registration.socket.gethostname", return_value="agent-host"),
        patch("agent_app.registration.get_local_ip", return_value="10.0.0.5"),
        patch("agent_app.registration.httpx.AsyncClient", return_value=client),
    ):
        result = await register_with_manager("http://manager:8000", 5100)

    assert result == {"id": "host-1", "status": "online"}
    payload = client.post.await_args.kwargs["json"]
    assert payload["hostname"] == "agent-host"
    assert payload["ip"] == "10.0.0.5"
    assert payload["agent_port"] == 5100
    assert payload["capabilities"]["platforms"] == ["android_mobile"]
    assert "auth" not in client.post.await_args.kwargs


async def test_register_with_manager_uses_basic_auth_when_configured() -> None:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    response = MagicMock(spec=httpx.Response)
    response.json.return_value = {"id": "host-1", "status": "online"}
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)

    with (
        patch(
            "agent_app.registration.get_or_refresh_capabilities_snapshot",
            new_callable=AsyncMock,
            return_value={"platforms": ["android_mobile"]},
        ),
        patch("agent_app.registration.socket.gethostname", return_value="agent-host"),
        patch("agent_app.registration.get_local_ip", return_value="10.0.0.5"),
        patch("agent_app.registration.httpx.AsyncClient", return_value=client),
        patch("agent_app.registration.agent_settings.manager.manager_auth_username", "machine"),
        patch("agent_app.registration.agent_settings.manager.manager_auth_password", "machine-secret"),
    ):
        result = await register_with_manager("http://manager:8000", 5100)

    assert result == {"id": "host-1", "status": "online"}
    auth = client.post.await_args.kwargs["auth"]
    assert isinstance(auth, httpx.BasicAuth)


def test_agent_settings_require_complete_manager_auth_pair() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        ManagerSettings(manager_auth_username="machine")


async def test_registration_loop_retries_after_4xx_rejection(caplog: pytest.LogCaptureFixture) -> None:
    request = httpx.Request("POST", "http://manager:8000/api/hosts/register")
    response = httpx.Response(422, text="capabilities missing field 'platform'", request=request)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch(
            "agent_app.registration.register_with_manager",
            new_callable=AsyncMock,
            side_effect=[
                httpx.HTTPStatusError("bad request", request=request, response=response),
                asyncio.CancelledError,
            ],
        ) as register,
        patch("agent_app.registration.asyncio.sleep", side_effect=fake_sleep),
        caplog.at_level(logging.WARNING, logger="agent_app.registration"),
        pytest.raises(asyncio.CancelledError),
    ):
        await registration_loop("http://manager:8000", 5100)

    assert register.await_count == 2
    assert sleeps == [300.0]
    assert any("capabilities missing field" in record.getMessage() for record in caplog.records)


async def test_registration_loop_retries_transport_failures_with_backoff() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if delay == 30.0:
            raise asyncio.CancelledError

    with (
        patch(
            "agent_app.registration.register_with_manager",
            new_callable=AsyncMock,
            side_effect=[httpx.ConnectError("down"), {"id": "host-1", "status": "online"}],
        ) as register,
        patch("agent_app.registration.asyncio.sleep", side_effect=fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await registration_loop("http://manager:8000", 5100)

    assert register.await_count == 2
    assert sleeps == [2.0, 30.0]


async def test_registration_loop_refreshes_successful_registration() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    host_identity = MagicMock()

    with (
        patch(
            "agent_app.registration.register_with_manager",
            new_callable=AsyncMock,
            side_effect=[
                {"id": "host-1", "status": "online"},
                {"id": "host-1", "status": "online"},
            ],
        ) as register,
        patch("agent_app.registration.asyncio.sleep", side_effect=fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await registration_loop("http://manager:8000", 5100, host_identity)

    assert register.await_count == 2
    assert sleeps == [30.0, 30.0]
    assert host_identity.set.call_args_list == [call("host-1"), call("host-1")]


async def test_registration_loop_notifies_when_advertised_ip_changes() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    on_ip_change = AsyncMock()

    with (
        patch(
            "agent_app.registration.register_with_manager",
            new_callable=AsyncMock,
            side_effect=[
                {"id": "host-1", "status": "online", "ip": "192.168.1.10"},
                {"id": "host-1", "status": "online", "ip": "192.168.88.107"},
            ],
        ),
        patch("agent_app.registration.asyncio.sleep", side_effect=fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await registration_loop("http://manager:8000", 5100, on_advertised_ip_change=on_ip_change)

    assert on_ip_change.await_args_list == [call("192.168.1.10"), call("192.168.88.107")]


async def test_register_with_manager_stores_version_guidance() -> None:
    clear_version_guidance()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    response = MagicMock(spec=httpx.Response)
    response.json.return_value = {
        "id": "host-1",
        "status": "online",
        "required_agent_version": "0.2.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "outdated",
        "agent_update_available": True,
    }
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)

    with (
        patch("agent_app.registration.get_or_refresh_capabilities_snapshot", new_callable=AsyncMock, return_value={}),
        patch("agent_app.registration.socket.gethostname", return_value="agent-host"),
        patch("agent_app.registration.get_local_ip", return_value="10.0.0.5"),
        patch("agent_app.registration.httpx.AsyncClient", return_value=client),
    ):
        await register_with_manager("http://manager:8000", 5100)

    guidance = get_version_guidance()
    assert guidance.required_agent_version == "0.2.0"
    assert guidance.recommended_agent_version == "0.3.0"
    assert guidance.agent_version_status == "outdated"


async def test_register_with_manager_sends_host_info() -> None:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    response = MagicMock(spec=httpx.Response)
    response.json.return_value = {"id": "host-1", "status": "online"}
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)

    hardware = {
        "os_version": "macOS 14.5",
        "kernel_version": "Darwin 23.5.0",
        "cpu_arch": "arm64",
        "cpu_model": "Apple M2 Pro",
        "cpu_cores": 12,
        "total_memory_mb": 32768,
        "total_disk_gb": 1024,
    }

    with (
        patch(
            "agent_app.registration.get_or_refresh_capabilities_snapshot",
            new_callable=AsyncMock,
            return_value={"platforms": [], "orchestration_contract_version": 2},
        ),
        patch("agent_app.registration.socket.gethostname", return_value="agent-host"),
        patch("agent_app.registration.get_local_ip", return_value="10.0.0.5"),
        patch("agent_app.registration.hardware_info.collect", return_value=hardware),
        patch("agent_app.registration.httpx.AsyncClient", return_value=client),
    ):
        await register_with_manager("http://manager:8000", 5100)

    payload = client.post.await_args.kwargs["json"]
    assert payload["host_info"] == hardware


async def test_register_with_manager_logs_upgrade_guidance_once(caplog: pytest.LogCaptureFixture) -> None:
    clear_version_guidance()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    response = MagicMock(spec=httpx.Response)
    response.json.return_value = {
        "id": "host-1",
        "status": "online",
        "required_agent_version": "0.2.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "outdated",
        "agent_update_available": True,
    }
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)

    with (
        patch("agent_app.registration.get_or_refresh_capabilities_snapshot", new_callable=AsyncMock, return_value={}),
        patch("agent_app.registration.socket.gethostname", return_value="agent-host"),
        patch("agent_app.registration.get_local_ip", return_value="10.0.0.5"),
        patch("agent_app.registration.httpx.AsyncClient", return_value=client),
        caplog.at_level("INFO"),
    ):
        await register_with_manager("http://manager:8000", 5100)
        await register_with_manager("http://manager:8000", 5100)

    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("Agent update available: recommended version is 0.3.0") == 1
