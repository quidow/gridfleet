from __future__ import annotations

from typing import Any

import pytest

from agent_app.pack.adapter_types import HealthCheckResult
from agent_app.pack.contexts import HealthCtx
from agent_app.pack.dispatch import _adapter_health_payload, adapter_health_check


def test_payload_lifts_recommended_action_to_top_level() -> None:
    results = [
        HealthCheckResult(check_id="adb_connected", ok=False, detail="down"),
        HealthCheckResult(check_id="link_repairable", ok=False, recommended_action="reconnect"),
    ]
    payload = _adapter_health_payload(results)
    assert payload["recommended_action"] == "reconnect"
    assert payload["healthy"] is False


def test_payload_has_no_recommended_action_key_when_none() -> None:
    results = [HealthCheckResult(check_id="adb_connected", ok=True)]
    payload = _adapter_health_payload(results)
    assert "recommended_action" not in payload


@pytest.mark.asyncio
async def test_health_ctx_carries_claimed_ports_and_live_flag() -> None:
    seen: dict[str, Any] = {}

    class _Adapter:
        pack_id = "appium-uiautomator2"
        pack_release = "1"

        async def health_check(self, ctx: object) -> list[HealthCheckResult]:
            seen["claimed_ports"] = getattr(ctx, "claimed_ports", None)
            seen["has_live_session"] = getattr(ctx, "has_live_session", None)
            return [HealthCheckResult(check_id="adb_connected", ok=True)]

    class _Reg:
        def get(self, pack_id: str, release: str) -> _Adapter:
            return _Adapter()

    await adapter_health_check(
        adapter_registry=_Reg(),  # type: ignore[arg-type]
        pack_id="appium-uiautomator2",
        pack_release="1",
        ctx=HealthCtx(
            device_identity_value="t",
            allow_boot=False,
            claimed_ports={"appium:systemPort": 8200},
            has_live_session=False,
        ),
    )
    assert seen["claimed_ports"] == {"appium:systemPort": 8200}
    assert seen["has_live_session"] is False


def test_parse_claimed_ports() -> None:
    from agent_app.pack.router import _parse_claimed_ports

    assert _parse_claimed_ports('{"appium:systemPort": 8200}') == {"appium:systemPort": 8200}
    assert _parse_claimed_ports(None) is None
    assert _parse_claimed_ports("") is None
    assert _parse_claimed_ports("not json") is None
    assert _parse_claimed_ports('["list"]') is None
    assert _parse_claimed_ports('{"k": "NaNport"}') is None
