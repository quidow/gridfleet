"""Reproducer for bug 8: ``HostIdentity.set`` rotation must propagate to
long-lived consumers. Captured-at-construction patterns leave them pinned
to the stale id; the fix is to hold a ``HostIdentity`` reference and
re-read on every iteration.

See ``docs/superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 8).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv, RuntimeSpec
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.state import PackStateLoop

if TYPE_CHECKING:
    from agent_app.pack.adapter_types import DoctorCheckResult, DoctorContext


async def test_host_identity_rotation_returns_consistent_value_to_all_waiters() -> None:
    hi = HostIdentity()

    async def early_waiter() -> str:
        return await hi.wait()

    task = asyncio.create_task(early_waiter())
    # Let the early waiter reach its `await self._event.wait()`.
    await asyncio.sleep(0)

    hi.set("host-a")
    # Yield once so the early waiter resumes and reads `_value` *before* the
    # second set() overwrites it.
    await asyncio.sleep(0)

    hi.set("host-b")

    early_value = await task
    late_value = await hi.wait()

    assert early_value == late_value


class _RotationFakeClient:
    def __init__(self, desired_payload: dict[str, Any]) -> None:
        self._desired = desired_payload

    async def fetch_desired(self) -> dict[str, Any]:
        return self._desired


class _NoopRuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        return {}, {}


class _SucceedingRuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        envs: dict[str, RuntimeEnv] = {}
        for pack_id, spec in desired_by_pack.items():
            rid = AppiumRuntimeManager.runtime_id_for(spec)
            envs[pack_id] = RuntimeEnv(
                runtime_id=rid,
                appium_home=f"/fake/{rid}",
                appium_bin=f"/fake/{rid}/node_modules/.bin/appium",
                server_package=spec.server_package,
                server_version=spec.server_version,
            )
        return envs, {}


@pytest.mark.asyncio
async def test_pack_state_loop_resolves_rotated_host_id_each_iteration() -> None:
    """PackStateLoop must read the live host_id each iteration (held by reference,
    not captured at construction). The status payload no longer carries host_id
    (see the consolidated status push), so this asserts on the resolver the loop
    itself uses; ``test_pack_state_loop_doctor_ctx_picks_up_rotated_host_id`` below
    covers the same rotation through an observable side effect."""

    identity = HostIdentity()
    identity.set("host-a")
    # ``host_id`` in the *desired* payload is unrelated to identity rotation —
    # it's required by ``parse_desired_payload`` but doesn't drive the agent's
    # outgoing host_id, so we pin it to a placeholder value.
    client = _RotationFakeClient({"host_id": "desired-placeholder", "packs": []})
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_NoopRuntimeMgr(),
        host_identity=identity,
    )

    await loop.run_once()
    assert loop._resolve_host_id() == "host-a"

    identity.set("host-b")

    await loop.run_once()
    assert loop._resolve_host_id() == "host-b"


class _DoctorRecordingAdapter:
    pack_id = "vendor-doctor"
    pack_release = "0.1.0"
    discovery_scope = ""

    def __init__(self) -> None:
        self.host_ids_seen: list[str] = []

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        self.host_ids_seen.append(ctx.host_id)
        return []


def _doctor_pack_payload() -> dict[str, Any]:
    return {
        "id": "vendor-doctor",
        "release": "0.1.0",
        "appium_server": {
            "source": "npm",
            "package": "appium",
            "version": ">=2.5,<3",
            "recommended": "2.11.5",
            "known_bad": [],
        },
        "appium_driver": {
            "source": "npm",
            "package": "vendor-driver",
            "version": ">=1,<2",
            "recommended": "1.0.0",
            "known_bad": [],
        },
        "platforms": [
            {
                "id": "vendor_real",
                "automation_name": "Vendor",
                "device_types": ["real_device"],
                "connection_types": ["usb"],
                "identity": {"scheme": "vendor_serial", "scope": "host"},
                "display_name": "Vendor",
                "appium_platform_name": "Vendor",
                "capabilities": {
                    "stereotype": {"appium:platformName": "Vendor"},
                    "session_required": [],
                },
            }
        ],
        "requires": {},
    }


@pytest.mark.asyncio
async def test_pack_state_loop_doctor_ctx_picks_up_rotated_host_id() -> None:
    """The DoctorCtx passed into dispatched adapter.doctor() must carry the
    live host_id, not the value captured at PackStateLoop construction.

    Doctor now only auto-runs on runtime change (not every iteration), so we
    clear the runtime_registry between runs to force a fresh install each time.
    """

    identity = HostIdentity()
    identity.set("host-a")
    registry = AdapterRegistry()
    adapter = _DoctorRecordingAdapter()
    registry.set("vendor-doctor", "0.1.0", adapter)  # type: ignore[arg-type]
    runtime_registry = RuntimeRegistry()

    loop = PackStateLoop(
        client=_RotationFakeClient({"host_id": "desired-placeholder", "packs": [_doctor_pack_payload()]}),
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=identity,
        adapter_registry=registry,
        runtime_registry=runtime_registry,
    )

    await loop.run_once()
    identity.set("host-b")
    runtime_registry.purge_except(set())
    await loop.run_once()

    assert adapter.host_ids_seen == ["host-a", "host-b"]
