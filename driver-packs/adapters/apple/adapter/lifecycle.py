"""Apple simulator lifecycle actions."""

from __future__ import annotations

from typing import Any

from agent_app.pack.adapter_types import LifecycleActionResult, LifecycleContext
from agent_app.pack.adapter_utils import run_cmd

from adapter.health import _simulator_state


async def lifecycle_action(action_id: str, args: dict[str, Any], ctx: LifecycleContext) -> LifecycleActionResult:
    udid = str(args.get("udid") or ctx.device_identity_value)
    if action_id == "state":
        state = await _simulator_state(udid)
        return LifecycleActionResult(ok=state is not None, state=state or "not_found")
    if action_id == "boot":
        await run_cmd(["xcrun", "simctl", "boot", udid], timeout=60)
        return LifecycleActionResult(ok=True, state="booted")
    if action_id == "shutdown":
        await run_cmd(["xcrun", "simctl", "shutdown", udid], timeout=15)
        return LifecycleActionResult(ok=True, state="shutdown")
    return LifecycleActionResult(ok=False, detail=f"Unsupported Apple lifecycle action: {action_id}")
