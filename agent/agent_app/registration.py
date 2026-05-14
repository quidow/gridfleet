"""Self-registration with the GridFleet backend."""

from __future__ import annotations

import asyncio
import logging
import platform
import socket
from typing import TYPE_CHECKING, Any

import httpx

from agent_app import __version__
from agent_app.config import agent_settings
from agent_app.grid_url import get_local_ip
from agent_app.host.capabilities import get_or_refresh_capabilities_snapshot
from agent_app.host.version_guidance import update_version_guidance
from agent_app.http_client import get_client as get_shared_http_client

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_app.pack.host_identity import HostIdentity

__all__ = ["get_local_ip", "register_with_manager", "registration_loop"]

logger = logging.getLogger(__name__)


def _map_os_type() -> str:
    """Map platform.system() to the OSType enum values."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    return "linux"


def _handle_version_guidance(data: dict[str, Any]) -> None:
    changed = update_version_guidance(data)
    update_available = data.get("agent_update_available")
    recommended = data.get("recommended_agent_version")
    if changed and update_available is True and isinstance(recommended, str) and recommended:
        logger.info("Agent update available: recommended version is %s", recommended)


def _manager_auth() -> httpx.BasicAuth | None:
    username = agent_settings.manager_auth_username
    password = agent_settings.manager_auth_password
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


async def register_with_manager(manager_url: str, agent_port: int) -> dict[str, Any] | None:
    """POST to /api/hosts/register. Returns response JSON on success, None on failure."""
    capabilities = await get_or_refresh_capabilities_snapshot()
    payload = {
        "hostname": socket.gethostname(),
        "ip": get_local_ip(),
        "os_type": _map_os_type(),
        "agent_port": agent_port,
        "agent_version": __version__,
        "capabilities": capabilities,
    }

    client = get_shared_http_client()
    request_kwargs: dict[str, Any] = {"json": payload, "timeout": 10}
    if (auth := _manager_auth()) is not None:
        request_kwargs["auth"] = auth
    resp = await client.post(f"{manager_url}/api/hosts/register", **request_kwargs)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    _handle_version_guidance(data)
    return data


async def registration_loop(
    manager_url: str,
    agent_port: int,
    host_identity: HostIdentity | None = None,
    *,
    refresh_interval: float | None = None,
    on_advertised_ip_change: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Background task: retry registration and periodically refresh mutable host fields."""
    delay = 2.0
    max_delay = 60.0
    refresh_delay = float(
        agent_settings.registration_refresh_interval_sec if refresh_interval is None else refresh_interval
    )
    last_advertised_ip: str | None = None

    while True:
        try:
            result = await register_with_manager(manager_url, agent_port)
            if result:
                logger.info(
                    "Registered with manager: host_id=%s status=%s",
                    result.get("id"),
                    result.get("status"),
                )
                if host_identity is not None and result is not None:
                    host_id = result.get("id")
                    if isinstance(host_id, str):
                        host_identity.set(host_id)
                advertised_ip = result.get("ip")
                if isinstance(advertised_ip, str) and advertised_ip and advertised_ip != last_advertised_ip:
                    last_advertised_ip = advertised_ip
                    if on_advertised_ip_change is not None:
                        await on_advertised_ip_change(advertised_ip)
                delay = 2.0
                await asyncio.sleep(refresh_delay)
                continue
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            logger.warning("Registration rejected by manager: %s", status_code)
            if 400 <= status_code < 500:
                return  # Don't retry on client-side registration errors
        except (httpx.HTTPError, OSError) as e:
            logger.warning("Registration failed (retrying in %.0fs): %s", delay, e)

        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)
