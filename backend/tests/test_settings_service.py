from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.config import settings as process_settings
from app.models.setting import Setting
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_enabling_terminal_without_token_raises_when_auth_on(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_settings, "auth_enabled", True)
    monkeypatch.setattr(process_settings, "agent_terminal_token", None)

    with pytest.raises(ValueError, match="GRIDFLEET_AGENT_TERMINAL_TOKEN"):
        await settings_service.update(db_session, "agent.enable_web_terminal", True)

    assert settings_service.get("agent.enable_web_terminal") is False
    rows = await db_session.execute(select(Setting).where(Setting.key == "agent.enable_web_terminal"))
    assert rows.scalar_one_or_none() is None


async def test_enabling_terminal_with_token_succeeds_when_auth_on(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_settings, "auth_enabled", True)
    monkeypatch.setattr(process_settings, "agent_terminal_token", "s3cret")

    result = await settings_service.update(db_session, "agent.enable_web_terminal", True)
    assert result["value"] is True
    assert settings_service.get("agent.enable_web_terminal") is True


async def test_enabling_terminal_without_token_succeeds_when_auth_off(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_settings, "auth_enabled", False)
    monkeypatch.setattr(process_settings, "agent_terminal_token", None)

    result = await settings_service.update(db_session, "agent.enable_web_terminal", True)
    assert result["value"] is True


async def test_bulk_update_rejects_terminal_enable_without_token(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_settings, "auth_enabled", True)
    monkeypatch.setattr(process_settings, "agent_terminal_token", None)

    with pytest.raises(ValueError, match="GRIDFLEET_AGENT_TERMINAL_TOKEN"):
        await settings_service.bulk_update(
            db_session,
            {
                "agent.enable_web_terminal": True,
                "agent.web_terminal_allowed_origins": "https://gridfleet.example",
            },
        )
    assert settings_service.get("agent.enable_web_terminal") is False
    assert settings_service.get("agent.web_terminal_allowed_origins") == ""
    rows = await db_session.execute(
        select(Setting).where(Setting.key.in_(["agent.enable_web_terminal", "agent.web_terminal_allowed_origins"]))
    )
    assert rows.scalars().all() == []
