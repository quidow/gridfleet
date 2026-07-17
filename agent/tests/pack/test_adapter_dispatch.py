"""Tests for ``agent_app.pack.adapter_dispatch``.

Uses a hand-rolled fake Adapter to cover:
- Happy path for each dispatch wrapper.
- Adapter exception → AdapterHookExecutionError.
- Wrong return type → AdapterContractError.
"""

from __future__ import annotations

from typing import Literal

import pytest

from agent_app.pack.adapter_dispatch import (
    AdapterContractError,
    AdapterHookExecutionError,
    adapter_supports,
    dispatch_discover,
    dispatch_doctor,
    dispatch_health_check,
    dispatch_lifecycle_action,
    dispatch_normalize_device,
    dispatch_post_session,
    dispatch_pre_session,
    dispatch_telemetry,
    missing_declared_hooks,
)
from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DoctorCheckResult,
    FieldError,
    HardwareTelemetry,
    HealthCheckResult,
    LifecycleActionResult,
    NormalizedDevice,
    SessionOutcome,
    SessionSpec,
)
from agent_app.pack.contexts import DiscoveryCtx, DoctorCtx, HealthCtx, LifecycleCtx, NormalizeCtx, TelemetryCtx
from agent_app.pack.manifest import DesiredPack, DesiredPlatform
from agent_app.pack.runtime_types import AppiumInstallable
from tests.pack.fake_worker import FakeWorkerHandle

# ---------------------------------------------------------------------------
# Fake adapter variants
# ---------------------------------------------------------------------------


class _GoodAdapter:
    """Returns correct types from every hook."""

    pack_id = "vendor-good"
    pack_release = "1.0.0"

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        return [
            DiscoveryCandidate(
                identity_scheme="adb_serial",
                identity_value="emulator-5554",
                suggested_name="Pixel 7",
                detected_properties={},
                runnable=True,
                missing_requirements=[],
                field_errors=[],
            )
        ]

    async def doctor(self, ctx: object) -> list[DoctorCheckResult]:
        return [DoctorCheckResult(check_id="adb_version", ok=True, message="adb 1.0.41")]

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        return [HealthCheckResult(check_id="device_online", ok=True, detail="online")]

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, object],
        ctx: object,
    ) -> LifecycleActionResult:
        return LifecycleActionResult(ok=True, state="running", detail="")

    async def pre_session(self, spec: object) -> dict[str, object]:
        return {"appium:vendorMagic": "enabled"}

    async def post_session(self, spec: object, outcome: object) -> None:
        return None

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        return NormalizedDevice(
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="avd:Pixel_7",
            connection_target="emulator-5554",
            ip_address="",
            device_type="emulator",
            connection_type="",
            os_version="14",
            field_errors=[],
        )

    async def telemetry(self, ctx: object) -> HardwareTelemetry:
        return HardwareTelemetry(supported=True, battery_level_percent=85)


class _RaisingAdapter:
    """Every hook raises a RuntimeError."""

    pack_id = "vendor-bad"
    pack_release = "1.0.0"

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        raise RuntimeError("discover exploded")

    async def doctor(self, ctx: object) -> list[DoctorCheckResult]:
        raise RuntimeError("doctor exploded")

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        raise RuntimeError("health_check exploded")

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, object],
        ctx: object,
    ) -> LifecycleActionResult:
        raise RuntimeError("lifecycle_action exploded")

    async def pre_session(self, spec: object) -> dict[str, object]:
        raise RuntimeError("pre_session exploded")

    async def post_session(self, spec: object, outcome: object) -> None:
        raise RuntimeError("post_session exploded")

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        raise RuntimeError("normalize_device exploded")

    async def telemetry(self, ctx: object) -> HardwareTelemetry:
        raise RuntimeError("telemetry exploded")


class _WrongTypeAdapter:
    """Every hook returns the wrong type to trigger AdapterContractError."""

    pack_id = "vendor-wrong"
    pack_release = "1.0.0"

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        return "not-a-list"  # type: ignore[return-value]

    async def doctor(self, ctx: object) -> list[DoctorCheckResult]:
        return 42  # type: ignore[return-value]

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        return {"oops": True}  # type: ignore[return-value]

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, object],
        ctx: object,
    ) -> LifecycleActionResult:
        return ["wrong", "type"]  # type: ignore[return-value]

    async def pre_session(self, spec: object) -> dict[str, object]:
        return ["not", "a", "dict"]  # type: ignore[return-value]

    async def post_session(self, spec: object, outcome: object) -> None:
        pass  # post_session returns None — no contract error possible

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        return {"bad": "type"}  # type: ignore[return-value]

    async def telemetry(self, ctx: object) -> HardwareTelemetry:
        return {"bad": "type"}  # type: ignore[return-value]


def test_normalized_device_dataclass() -> None:
    nd = NormalizedDevice(
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="ABC123",
        connection_target="ABC123",
        ip_address="",
        device_type="real_device",
        connection_type="usb",
        os_version="14",
        field_errors=[FieldError(field_id="example", message="message")],
    )
    assert nd.identity_scheme == "android_serial"


def test_hardware_telemetry_unsupported() -> None:
    telemetry = HardwareTelemetry(supported=False)
    assert telemetry.battery_level_percent is None


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_discover_returns_list() -> None:
    adapter = _GoodAdapter()
    results = await dispatch_discover(
        FakeWorkerHandle(adapter), DiscoveryCtx(host_id="host-1", platform_id="android_mobile")
    )
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0].identity_value == "emulator-5554"


@pytest.mark.asyncio
async def test_dispatch_doctor_returns_list() -> None:
    adapter = _GoodAdapter()
    results = await dispatch_doctor(FakeWorkerHandle(adapter), DoctorCtx(host_id="host-1"))
    assert isinstance(results, list)
    assert results[0].check_id == "adb_version"
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_dispatch_health_check_returns_list() -> None:
    adapter = _GoodAdapter()
    results = await dispatch_health_check(
        FakeWorkerHandle(adapter), HealthCtx(device_identity_value="emulator-5554", allow_boot=False)
    )
    assert isinstance(results, list)
    assert results[0].check_id == "device_online"
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_dispatch_lifecycle_action_returns_result() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_lifecycle_action(
        FakeWorkerHandle(adapter),
        "reconnect",
        {},
        LifecycleCtx(host_id="host-1", device_identity_value="emulator-5554"),
    )
    assert isinstance(result, LifecycleActionResult)
    assert result.ok is True
    assert result.state == "running"


@pytest.mark.asyncio
async def test_dispatch_pre_session_returns_dict() -> None:
    adapter = _GoodAdapter()
    spec = SessionSpec(
        pack_id="vendor-good",
        platform_id="android_mobile",
        device_identity_value="emulator-5554",
    )
    result = await dispatch_pre_session(FakeWorkerHandle(adapter), spec)
    assert isinstance(result, dict)
    assert result["appium:vendorMagic"] == "enabled"


@pytest.mark.asyncio
async def test_dispatch_post_session_returns_none() -> None:
    adapter = _GoodAdapter()
    spec = SessionSpec(
        pack_id="vendor-good",
        platform_id="android_mobile",
        device_identity_value="emulator-5554",
    )
    outcome = SessionOutcome(ok=True)
    # Should not raise
    result = await dispatch_post_session(FakeWorkerHandle(adapter), spec, outcome)
    assert result is None


# ---------------------------------------------------------------------------
# Execution error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_discover_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_discover(FakeWorkerHandle(adapter), DiscoveryCtx(host_id="host-1", platform_id="android_mobile"))
    assert exc_info.value.hook == "discover"
    assert "discover exploded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_doctor_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_doctor(FakeWorkerHandle(adapter), DoctorCtx(host_id="host-1"))
    assert exc_info.value.hook == "doctor"


@pytest.mark.asyncio
async def test_dispatch_health_check_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_health_check(
            FakeWorkerHandle(adapter), HealthCtx(device_identity_value="emulator-5554", allow_boot=False)
        )
    assert exc_info.value.hook == "health_check"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_action_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_lifecycle_action(
            FakeWorkerHandle(adapter),
            "reconnect",
            {},
            LifecycleCtx(host_id="host-1", device_identity_value="emulator-5554"),
        )
    assert exc_info.value.hook == "lifecycle_action"


@pytest.mark.asyncio
async def test_dispatch_pre_session_execution_error() -> None:
    adapter = _RaisingAdapter()
    spec = SessionSpec(
        pack_id="vendor-bad",
        platform_id="android_mobile",
        device_identity_value="emulator-5554",
    )
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_pre_session(FakeWorkerHandle(adapter), spec)
    assert exc_info.value.hook == "pre_session"


@pytest.mark.asyncio
async def test_dispatch_post_session_execution_error() -> None:
    adapter = _RaisingAdapter()
    spec = SessionSpec(
        pack_id="vendor-bad",
        platform_id="android_mobile",
        device_identity_value="emulator-5554",
    )
    outcome = SessionOutcome(ok=False, detail="failed")
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_post_session(FakeWorkerHandle(adapter), spec, outcome)
    assert exc_info.value.hook == "post_session"


# ---------------------------------------------------------------------------
# Contract error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_discover_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_discover(FakeWorkerHandle(adapter), DiscoveryCtx(host_id="host-1", platform_id="android_mobile"))
    assert exc_info.value.hook == "discover"
    assert "list" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_doctor_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_doctor(FakeWorkerHandle(adapter), DoctorCtx(host_id="host-1"))
    assert exc_info.value.hook == "doctor"


@pytest.mark.asyncio
async def test_dispatch_health_check_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_health_check(
            FakeWorkerHandle(adapter), HealthCtx(device_identity_value="emulator-5554", allow_boot=False)
        )
    assert exc_info.value.hook == "health_check"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_action_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_lifecycle_action(
            FakeWorkerHandle(adapter),
            "state",
            {},
            LifecycleCtx(host_id="host-1", device_identity_value="emulator-5554"),
        )
    assert exc_info.value.hook == "lifecycle_action"
    assert "LifecycleActionResult" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_pre_session_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    spec = SessionSpec(
        pack_id="vendor-wrong",
        platform_id="android_mobile",
        device_identity_value="emulator-5554",
    )
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_pre_session(FakeWorkerHandle(adapter), spec)
    assert exc_info.value.hook == "pre_session"
    assert "dict" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Error attribute tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_error_chained_cause() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_discover(FakeWorkerHandle(adapter), DiscoveryCtx(host_id="host-1", platform_id="android_mobile"))
    err = exc_info.value
    assert err.__cause__ is not None
    assert isinstance(err.__cause__, RuntimeError)
    assert err.pack_id == "vendor-bad"
    assert err.pack_release == "1.0.0"


# ---------------------------------------------------------------------------
# dispatch_normalize_device / dispatch_telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_normalize_device_returns_device() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_normalize_device(
        FakeWorkerHandle(adapter),
        NormalizeCtx(host_id="host-1", platform_id="android_mobile", raw_input={"connection_target": "emulator-5554"}),
    )
    assert isinstance(result, NormalizedDevice)
    assert result.identity_value == "avd:Pixel_7"


@pytest.mark.asyncio
async def test_dispatch_telemetry_returns_telemetry() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_telemetry(
        FakeWorkerHandle(adapter),
        TelemetryCtx(device_identity_value="emulator-5554", connection_target="emulator-5554"),
    )
    assert isinstance(result, HardwareTelemetry)
    assert result.battery_level_percent == 85


@pytest.mark.asyncio
async def test_dispatch_normalize_device_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_normalize_device(
            FakeWorkerHandle(adapter),
            NormalizeCtx(
                host_id="host-1", platform_id="android_mobile", raw_input={"connection_target": "emulator-5554"}
            ),
        )
    assert exc_info.value.hook == "normalize_device"


@pytest.mark.asyncio
async def test_dispatch_telemetry_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_telemetry(
            FakeWorkerHandle(adapter),
            TelemetryCtx(device_identity_value="emulator-5554", connection_target="emulator-5554"),
        )
    assert exc_info.value.hook == "telemetry"


@pytest.mark.asyncio
async def test_dispatch_normalize_device_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_normalize_device(
            FakeWorkerHandle(adapter),
            NormalizeCtx(
                host_id="host-1", platform_id="android_mobile", raw_input={"connection_target": "emulator-5554"}
            ),
        )
    assert exc_info.value.hook == "normalize_device"
    assert "NormalizedDevice" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_telemetry_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_telemetry(
            FakeWorkerHandle(adapter),
            TelemetryCtx(device_identity_value="emulator-5554", connection_target="emulator-5554"),
        )
    assert exc_info.value.hook == "telemetry"
    assert "HardwareTelemetry" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Optional-hook capability probe + manifest-vs-hooks cross-check
# ---------------------------------------------------------------------------


class _MinimalAdapter:
    """Implements only the required core: discover + normalize_device."""

    pack_id = "vendor-minimal"
    pack_release = "1.0.0"

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        return []

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        raise NotImplementedError


def _installable() -> AppiumInstallable:
    return AppiumInstallable(source="npm", package="appium", version="2.0.0", recommended=None, known_bad=[])


def _pack_declaring_capabilities() -> DesiredPack:
    return DesiredPack(
        id="vendor-minimal",
        release="1.0.0",
        appium_server=_installable(),
        appium_driver=_installable(),
        platforms=[
            DesiredPlatform(
                id="p",
                automation_name="a",
                device_types=["real_device"],
                connection_types=["usb"],
                identity_scheme="s",
                identity_scope="host",
                stereotype={},
                lifecycle_actions=[{"id": "reconnect"}],
            )
        ],
    )


def test_adapter_supports_probes_real_methods() -> None:
    handle = FakeWorkerHandle(_MinimalAdapter())
    assert adapter_supports(handle, "discover") is True
    assert adapter_supports(handle, "normalize_device") is True
    assert adapter_supports(handle, "health_check") is False
    assert adapter_supports(handle, "lifecycle_action") is False


def test_adapter_supports_requires_worker_handshake() -> None:
    with pytest.raises(AttributeError):
        adapter_supports(_MinimalAdapter(), "discover")  # type: ignore[arg-type]


def test_missing_declared_hooks_reports_unimplemented() -> None:
    missing = missing_declared_hooks(_pack_declaring_capabilities(), FakeWorkerHandle(_MinimalAdapter()))
    assert missing == ["lifecycle_action"]


def test_missing_declared_hooks_empty_when_adapter_implements_them() -> None:
    assert missing_declared_hooks(_pack_declaring_capabilities(), FakeWorkerHandle(_GoodAdapter())) == []


def test_missing_declared_hooks_sees_device_type_override_lifecycle_actions() -> None:
    """lifecycle_actions declared only under device_type_overrides (e.g. the
    xcuitest simulator override) still require the lifecycle_action hook."""
    pack = DesiredPack(
        id="override-only",
        release="1.0.0",
        appium_server=_installable(),
        appium_driver=_installable(),
        platforms=[
            DesiredPlatform(
                id="p",
                automation_name="a",
                device_types=["real_device", "simulator"],
                connection_types=["usb"],
                identity_scheme="s",
                identity_scope="host",
                stereotype={},
                device_type_overrides={"simulator": {"lifecycle_actions": [{"id": "boot"}]}},
            )
        ],
    )
    assert missing_declared_hooks(pack, FakeWorkerHandle(_MinimalAdapter())) == ["lifecycle_action"]


def test_missing_declared_hooks_empty_when_manifest_declares_nothing() -> None:
    pack = DesiredPack(
        id="core-only",
        release="1.0.0",
        appium_server=_installable(),
        appium_driver=_installable(),
        platforms=[
            DesiredPlatform(
                id="p",
                automation_name="a",
                device_types=["real_device"],
                connection_types=["usb"],
                identity_scheme="s",
                identity_scope="host",
                stereotype={},
            )
        ],
    )
    assert missing_declared_hooks(pack, FakeWorkerHandle(_MinimalAdapter())) == []
