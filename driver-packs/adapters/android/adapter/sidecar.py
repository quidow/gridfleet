"""ADB monitor sidecar placeholder."""

from __future__ import annotations

from typing import Literal

from agent_app.pack.adapter_types import SidecarStatus


async def sidecar_lifecycle(action: Literal["start", "stop", "status"]) -> SidecarStatus:
    if action == "status":
        return SidecarStatus(ok=True, state="stopped", detail="ADB monitor sidecar is not running")
    if action in {"start", "stop"}:
        return SidecarStatus(ok=True, state="stopped")
    return SidecarStatus(ok=False, detail=f"Unknown sidecar action: {action}")
