"""Throwaway file used to verify pr-agent flags CLAUDE.md invariant violations.

Reverted in the next commit on this branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _Device:
    operational_state: str = ""


async def bad_state_write(session: AsyncSession, device: _Device, pack_id: str) -> None:
    if pack_id == "appium-uiautomator2":
        device.operational_state = "available"
    await session.commit()
