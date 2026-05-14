from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app import main
from app.agent_comm import agent_settings
from app.auth import auth_settings
from app.settings import settings_service
from app.settings.models import Setting

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_enabling_terminal_without_token_raises_when_auth_on(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_settings, "auth_enabled", True)
    monkeypatch.setattr(agent_settings, "agent_terminal_token", None)

    with pytest.raises(ValueError, match="GRIDFLEET_AGENT_TERMINAL_TOKEN"):
        await settings_service.update(db_session, "agent.enable_web_terminal", True)

    assert settings_service.get("agent.enable_web_terminal") is False
    rows = await db_session.execute(select(Setting).where(Setting.key == "agent.enable_web_terminal"))
    assert rows.scalar_one_or_none() is None


async def test_enabling_terminal_with_token_succeeds_when_auth_on(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_settings, "auth_enabled", True)
    monkeypatch.setattr(agent_settings, "agent_terminal_token", "s3cret")

    result = await settings_service.update(db_session, "agent.enable_web_terminal", True)
    assert result["value"] is True
    assert settings_service.get("agent.enable_web_terminal") is True


async def test_enabling_terminal_without_token_succeeds_when_auth_off(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_settings, "auth_enabled", False)
    monkeypatch.setattr(agent_settings, "agent_terminal_token", None)

    result = await settings_service.update(db_session, "agent.enable_web_terminal", True)
    assert result["value"] is True


async def test_bulk_update_rejects_terminal_enable_without_token(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_settings, "auth_enabled", True)
    monkeypatch.setattr(agent_settings, "agent_terminal_token", None)

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


async def test_leader_keepalive_interval_must_leave_stale_threshold_margin(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="leader_stale_threshold_sec"):
        await settings_service.update(db_session, "general.leader_keepalive_interval_sec", 60)

    assert settings_service.get("general.leader_keepalive_interval_sec") == 5


async def test_bulk_update_rejects_leader_keepalive_without_stale_threshold_margin(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="leader_stale_threshold_sec"):
        await settings_service.bulk_update(
            db_session,
            {
                "general.leader_keepalive_interval_sec": 20,
                "general.leader_stale_threshold_sec": 30,
            },
        )

    assert settings_service.get("general.leader_keepalive_interval_sec") == 5
    assert settings_service.get("general.leader_stale_threshold_sec") == 30


def test_startup_rejects_leader_keepalive_without_stale_threshold_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "general.leader_keepalive_interval_sec": 60,
        "general.leader_stale_threshold_sec": 30,
    }
    monkeypatch.setattr(settings_service, "get", lambda key: values[key])

    with pytest.raises(RuntimeError, match="leader_stale_threshold_sec"):
        main._validate_leader_keepalive_settings()

    values["general.leader_stale_threshold_sec"] = 120
    main._validate_leader_keepalive_settings()


# ── float validation tests (device_checks.ip_ping.timeout_sec: min=0.5, max=30.0) ──

_FLOAT_KEY = "device_checks.ip_ping.timeout_sec"


def test_validate_float_setting_accepts_valid_float() -> None:
    assert settings_service._validate_value(_FLOAT_KEY, 2.0) is None


def test_validate_float_setting_accepts_int_as_float() -> None:
    # An int (non-bool) is a valid float value
    assert settings_service._validate_value(_FLOAT_KEY, 5) is None


def test_validate_float_setting_rejects_non_numeric_string() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, "fast")
    assert error is not None
    assert "Expected float" in error


def test_validate_float_setting_rejects_bool() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, True)
    assert error is not None
    assert "Expected float" in error


def test_validate_float_setting_rejects_below_min() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, 0.1)
    assert error is not None
    assert "below minimum" in error


def test_validate_float_setting_rejects_above_max() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, 60.0)
    assert error is not None
    assert "exceeds maximum" in error


def test_validate_float_setting_rejects_nan() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, float("nan"))
    assert error is not None
    assert "finite" in error


def test_validate_float_setting_rejects_positive_infinity() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, float("inf"))
    assert error is not None
    assert "finite" in error


def test_validate_float_setting_rejects_negative_infinity() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, float("-inf"))
    assert error is not None
    assert "finite" in error
