"""Reproducer for bug 9: ``PackStateLoop.run_once`` skips ``post_status``
entirely when ``SidecarSupervisor.start`` raises. The backend reconciler
therefore never sees the failed sidecar and cannot react.

See ``docs/superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.runtime import RuntimeEnv, RuntimeSpec
from agent_app.pack.state import PackStateLoop

if TYPE_CHECKING:
    import pytest


def _host_identity(value: str) -> HostIdentity:
    hi = HostIdentity()
    hi.set(value)
    return hi


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.posted: list[dict[str, Any]] = []

    async def fetch_desired(self) -> dict[str, Any]:
        return self._payload

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted.append(payload)


class _FakeRuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        envs = {
            pack_id: RuntimeEnv(
                runtime_id="rid",
                appium_home="/tmp/appium-home",
                appium_bin="/tmp/appium-home/bin/appium",
                server_package="appium",
                server_version="2.11.5",
            )
            for pack_id in desired_by_pack
        }
        return envs, {}


class _FakeAdapter:
    pack_release = "2026.04.0"

    # A pack declaring a sidecar feature must ship a sidecar-capable adapter,
    # or the manifest-vs-hooks load cross-check blocks it. The supervisor mock
    # is what actually runs start/stop here, so this hook is never invoked.
    async def sidecar_lifecycle(self, feature_id: str, action: str) -> object:
        raise NotImplementedError


class _FakeAdapterRegistry:
    def has(self, pack_id: str, release: str) -> bool:
        return True

    def get(self, pack_id: str, release: str) -> _FakeAdapter:
        return _FakeAdapter()

    def get_current(self, pack_id: str) -> _FakeAdapter:
        return _FakeAdapter()


class _RaisingStartSidecarSupervisor:
    async def start(self, *, pack_id: str, release: str, feature_id: str, adapter: object) -> None:
        raise RuntimeError("sidecar boot failed")

    async def stop(self, *, pack_id: str, release: str, feature_id: str, adapter: object) -> None:
        pass

    async def drop(self, *, pack_id: str, release: str, feature_id: str) -> None:
        pass

    def tracked_keys(self) -> set[tuple[str, str, str]]:
        return set()

    def status_snapshot(self) -> list[dict[str, Any]]:
        return []


class _RaisingStopSidecarSupervisor:
    # Tracks a sidecar key that is NOT in the desired set so the stale-cleanup
    # branch runs and stop() raises.
    _STALE_KEY = ("appium-uiautomator2", "2026.04.0", "stale-feature")

    async def start(self, *, pack_id: str, release: str, feature_id: str, adapter: object) -> None:
        pass

    async def stop(self, *, pack_id: str, release: str, feature_id: str, adapter: object) -> None:
        raise RuntimeError("sidecar teardown failed")

    async def drop(self, *, pack_id: str, release: str, feature_id: str) -> None:
        pass

    def tracked_keys(self) -> set[tuple[str, str, str]]:
        return {self._STALE_KEY}

    def status_snapshot(self) -> list[dict[str, Any]]:
        return []


def _desired_payload() -> dict[str, Any]:
    return {
        "host_id": "00000000-0000-0000-0000-000000000001",
        "packs": [
            {
                "id": "appium-uiautomator2",
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
                "features": {"feat-1": {"sidecar": {"command": "noop"}, "actions": []}},
                "runtime_policy": {"strategy": "recommended"},
            }
        ],
    }


async def test_run_once_posts_status_when_sidecar_start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_doctor(*_args: object, **_kwargs: object) -> list[Any]:
        return []

    monkeypatch.setattr("agent_app.pack.state.dispatch_doctor", _no_doctor)

    client = _FakeClient(_desired_payload())
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
        adapter_registry=_FakeAdapterRegistry(),  # type: ignore[arg-type]
        sidecar_supervisor=_RaisingStartSidecarSupervisor(),  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert client.posted, "post_status must run even when sidecar.start raises"
    doctor = client.posted[0]["doctor"]
    assert any(entry["check_id"] == "sidecar_start:feat-1" and not entry["ok"] for entry in doctor)


async def test_run_once_posts_status_when_sidecar_stop_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_doctor(*_args: object, **_kwargs: object) -> list[Any]:
        return []

    monkeypatch.setattr("agent_app.pack.state.dispatch_doctor", _no_doctor)

    client = _FakeClient(_desired_payload())
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
        adapter_registry=_FakeAdapterRegistry(),  # type: ignore[arg-type]
        sidecar_supervisor=_RaisingStopSidecarSupervisor(),  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert client.posted, "post_status must run even when sidecar.stop raises"
    doctor = client.posted[0]["doctor"]
    assert any(entry["check_id"] == "sidecar_stop:stale-feature" and not entry["ok"] for entry in doctor)
