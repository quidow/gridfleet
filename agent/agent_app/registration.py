"""Self-registration with the GridFleet backend."""

from __future__ import annotations

import asyncio
import logging
import platform
import socket
from typing import TYPE_CHECKING, Any

import httpx2 as httpx

from agent_app.config import agent_settings, secret_value
from agent_app.grid_url import get_local_ip
from agent_app.host import hardware_info
from agent_app.http_client import get_client as get_shared_http_client
from agent_app.observability import sanitize_log_value

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_app.host.capabilities import CapabilitiesCache
    from agent_app.host.version_guidance import VersionGuidanceStore
    from agent_app.pack.host_identity import HostIdentity

__all__ = ["RegistrationService", "get_local_ip", "manager_auth"]

logger = logging.getLogger(__name__)


def _map_os_type() -> str:
    """Map platform.system() to the OSType enum values."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    return "linux"


def manager_auth() -> httpx.BasicAuth | None:
    username = agent_settings.manager.manager_auth_username
    password = secret_value(agent_settings.manager.manager_auth_password)
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


class RegistrationService:
    """Registers this agent with the manager and refreshes mutable host fields."""

    def __init__(
        self,
        *,
        capabilities_cache: CapabilitiesCache,
        version_guidance: VersionGuidanceStore,
        host_identity: HostIdentity | None = None,
        on_advertised_ip_change: Callable[[str], Awaitable[None]] | None = None,
        refresh_interval: float | None = None,
        boot_id: str | None = None,
    ) -> None:
        self._capabilities_cache = capabilities_cache
        self._version_guidance = version_guidance
        self._host_identity = host_identity
        self._on_advertised_ip_change = on_advertised_ip_change
        self._refresh_interval = refresh_interval
        self._boot_id = boot_id

    def _handle_version_guidance(self, data: dict[str, Any]) -> None:
        changed = self._version_guidance.update(data)
        update_available = data.get("agent_update_available")
        recommended = data.get("recommended_agent_version")
        if changed and update_available is True and isinstance(recommended, str) and recommended:
            logger.info("Agent update available: recommended version is %s", recommended)

    async def register_once(self, manager_url: str, agent_port: int) -> dict[str, Any] | None:
        """POST to /api/hosts/register. Returns response JSON on success, raises on HTTP error."""
        capabilities = await self._capabilities_cache.get_or_refresh()
        payload = {
            "hostname": socket.gethostname(),
            "ip": get_local_ip(),
            "os_type": _map_os_type(),
            "agent_port": agent_port,
            "capabilities": capabilities,
            "host_info": hardware_info.collect(),
            "boot_id": self._boot_id,
        }

        client = get_shared_http_client()
        request_kwargs: dict[str, Any] = {"json": payload, "timeout": 10}
        if (auth := manager_auth()) is not None:
            request_kwargs["auth"] = auth
        resp = await client.post(f"{manager_url}/api/hosts/register", **request_kwargs)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        self._handle_version_guidance(data)
        return data

    async def run(self, manager_url: str, agent_port: int) -> None:
        """Background task: retry registration and periodically refresh mutable host fields."""
        delay = 2.0
        max_delay = 60.0
        refresh_delay = float(
            agent_settings.core.registration_refresh_interval_sec
            if self._refresh_interval is None
            else self._refresh_interval
        )
        last_advertised_ip: str | None = None

        while True:
            try:
                result = await self.register_once(manager_url, agent_port)
                if result:
                    logger.info(
                        "Registered with manager: host_id=%s status=%s",
                        result.get("id"),
                        result.get("status"),
                    )
                    if self._host_identity is not None:
                        host_id = result.get("id")
                        if isinstance(host_id, str):
                            self._host_identity.set(host_id)
                    advertised_ip = result.get("ip")
                    if isinstance(advertised_ip, str) and advertised_ip and advertised_ip != last_advertised_ip:
                        last_advertised_ip = advertised_ip
                        if self._on_advertised_ip_change is not None:
                            await self._on_advertised_ip_change(advertised_ip)
                    delay = 2.0
                    await asyncio.sleep(refresh_delay)
                    continue
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                try:
                    body_excerpt = sanitize_log_value(e.response.text)
                except Exception:
                    body_excerpt = "<unreadable>"
                if 400 <= status_code < 500:
                    logger.warning(
                        "Registration rejected by manager (4xx, will retry): %s body=%s",
                        status_code,
                        body_excerpt,
                    )
                    await asyncio.sleep(300.0)
                    continue
                logger.warning("Registration error from manager: %s body=%s", status_code, body_excerpt)
            except (httpx.HTTPError, OSError) as e:
                logger.warning("Registration failed (retrying in %.0fs): %s", delay, e)

            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
