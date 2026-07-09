"""Reproducer for bug 11: ``RuntimeRegistry`` never sheds packs that the
backend has retired. ``PackStateLoop.run_once`` only writes via
``set_for_pack``; there is no purge step, so a desired→undesired transition
leaves stale RuntimeEnv entries that ``resolve_appium_invocation_for_pack``
will happily hand out.

See ``docs/superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 11).
"""

from __future__ import annotations

from typing import Any

from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.runtime import RuntimeEnv, RuntimeSpec
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.state import PackStateLoop


def _host_identity(value: str) -> HostIdentity:
    hi = HostIdentity()
    hi.set(value)
    return hi


class _StaticClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def fetch_desired(self) -> dict[str, Any]:
        return self._payload


class _FakeRuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        envs = {
            pack_id: RuntimeEnv(
                runtime_id=f"rid-{pack_id}",
                appium_home=f"/tmp/{pack_id}",
                appium_bin=f"/tmp/{pack_id}/bin/appium",
                server_package="appium",
                server_version="2.11.5",
            )
            for pack_id in desired_by_pack
        }
        return envs, {}


def _payload_with(packs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"host_id": "00000000-0000-0000-0000-000000000001", "packs": packs}


def _pack_entry(pack_id: str) -> dict[str, Any]:
    return {
        "id": pack_id,
        "release": "2026.04.0",
        "appium_server": {
            "source": "npm",
            "package": "appium",
            "version": ">=2.5,<3",
            "recommended": "2.11.5",
            "known_bad": [],
        },
        "appium_driver": {
            "source": "npm",
            "package": "appium-uiautomator2-driver",
            "version": ">=3,<5",
            "recommended": "3.6.0",
            "known_bad": [],
        },
        "platforms": [
            {
                "id": "android_mobile",
                "automation_name": "UiAutomator2",
                "device_types": ["real_device"],
                "connection_types": ["usb"],
                "identity": {"scheme": "android_serial", "scope": "host"},
                "appium_platform_name": "Android",
                "capabilities": {
                    "stereotype": {"appium:platformName": "Android"},
                    "session_required": [],
                },
            }
        ],
        "runtime_policy": {"strategy": "recommended"},
    }


async def test_runtime_registry_purges_packs_no_longer_desired() -> None:
    registry = RuntimeRegistry()
    client = _StaticClient(_payload_with([_pack_entry("pack-1")]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
        runtime_registry=registry,
    )

    await loop.run_once()
    assert registry.get_for_pack("pack-1") is not None

    # Backend drops pack-1 from desired state.
    client.set_payload(_payload_with([]))
    await loop.run_once()

    assert registry.get_for_pack("pack-1") is None


class _FlakyRuntimeMgr:
    """Succeeds first, then raises on subsequent reconciles."""

    def __init__(self) -> None:
        self.calls = 0

    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        self.calls += 1
        if self.calls == 1:
            envs = {
                pack_id: RuntimeEnv(
                    runtime_id=f"rid-{pack_id}",
                    appium_home=f"/tmp/{pack_id}",
                    appium_bin=f"/tmp/{pack_id}/bin/appium",
                    server_package="appium",
                    server_version="2.11.5",
                )
                for pack_id in desired_by_pack
            }
            return envs, {}
        raise RuntimeError("transient install failure")


async def test_runtime_registry_keeps_desired_packs_when_reconcile_fails() -> None:
    """Regression: a transient runtime_mgr failure must not evict cached envs
    for packs the backend still desires. Purge keyed on desired set, not on
    successful-install set.
    """

    registry = RuntimeRegistry()
    client = _StaticClient(_payload_with([_pack_entry("pack-1")]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FlakyRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
        runtime_registry=registry,
    )

    await loop.run_once()
    assert registry.get_for_pack("pack-1") is not None

    # Backend still desires pack-1, but reconcile throws this iteration.
    await loop.run_once()

    assert registry.get_for_pack("pack-1") is not None
