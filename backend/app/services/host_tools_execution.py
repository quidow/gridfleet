from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.services import control_plane_state_store
from app.services.agent_operations import ensure_tools as ensure_agent_tools
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

HOST_TOOLS_ENSURE_NAMESPACE = "host.tools.ensure"


def utcnow() -> datetime:
    return datetime.now(UTC)


def configured_tool_versions() -> tuple[str | None, str | None]:
    appium_version = settings_service.get("appium.target_version")
    selenium_version = settings_service.get("grid.selenium_jar_version")
    appium_target = appium_version.strip() if isinstance(appium_version, str) else ""
    selenium_target = selenium_version.strip() if isinstance(selenium_version, str) else ""
    return appium_target or None, selenium_target or None


async def store_tool_ensure_result(db: AsyncSession, host_id: uuid.UUID, payload: dict[str, Any]) -> None:
    await control_plane_state_store.set_value(
        db,
        HOST_TOOLS_ENSURE_NAMESPACE,
        str(host_id),
        {
            "recorded_at": utcnow().isoformat(),
            **payload,
        },
    )


async def _ensure_host_tools_versions(
    db: AsyncSession,
    host: Host,
    *,
    appium_version: str | None,
    selenium_version: str | None,
) -> dict[str, Any] | None:
    if appium_version is None and selenium_version is None:
        return None
    result = await ensure_agent_tools(
        host.ip,
        host.agent_port,
        appium_version=appium_version,
        selenium_jar_version=selenium_version,
    )
    await store_tool_ensure_result(db, host.id, {"result": result})
    await db.commit()
    return result
