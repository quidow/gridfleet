"""Tests for ``agent_app.pack.adapter_dispatch``.

Uses a hand-rolled fake Adapter to cover:
- Happy path for each dispatch wrapper.
- Timeout → AdapterHookTimeoutError.
- Adapter exception → AdapterHookExecutionError.
- Wrong return type → AdapterContractError.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar, Literal

import pytest

from agent_app.pack.adapter_dispatch import (
    ADAPTER_HOOK_TIMEOUT_SECONDS,
    AdapterContractError,
    AdapterHookExecutionError,
    AdapterHookTimeoutError,
    dispatch_discover,
    dispatch_doctor,
    dispatch_feature_action,
    dispatch_health_check,
    dispatch_lifecycle_action,
    dispatch_normalize_device,
    dispatch_post_session,
    dispatch_pre_session,
    dispatch_sidecar_lifecycle,
    dispatch_telemetry,
)
from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DoctorCheckResult,
    FeatureActionResult,
    FieldError,
    HardwareTelemetry,
    HealthCheckResult,
    LifecycleActionResult,
    NormalizedDevice,
    SessionOutcome,
    SessionSpec,
    SidecarStatus,
)

# ---------------------------------------------------------------------------
# Minimal context stubs
# ---------------------------------------------------------------------------


class _DiscoveryCtx:
    host_id = "host-1"
    platform_id = "android_mobile"


class _HealthCtx:
    device_identity_value = "emulator-5554"
    allow_boot = False


class _DoctorCtx:
    host_id = "host-1"


class _LifecycleCtx:
    host_id = "host-1"
    device_identity_value = "emulator-5554"


class _NormalizeCtx:
    host_id = "host-1"
    platform_id = "android_mobile"
    raw_input: ClassVar[dict[str, str]] = {"connection_target": "emulator-5554"}


class _TelemetryCtx:
    device_identity_value = "emulator-5554"
    connection_target = "emulator-5554"


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
                feature_status=[],
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

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, object],
        ctx: object,
    ) -> FeatureActionResult:
        return FeatureActionResult(ok=True, detail="done", data={"feature": feature_id})

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        return SidecarStatus(ok=True, detail="running", state="active")

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


class _TimeoutAdapter:
    """Every hook sleeps longer than the timeout deadline."""

    pack_id = "vendor-slow"
    pack_release = "1.0.0"

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return []  # pragma: no cover

    async def doctor(self, ctx: object) -> list[DoctorCheckResult]:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return []  # pragma: no cover

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return []  # pragma: no cover

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, object],
        ctx: object,
    ) -> LifecycleActionResult:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return LifecycleActionResult(ok=False)  # pragma: no cover

    async def pre_session(self, spec: object) -> dict[str, object]:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return {}  # pragma: no cover

    async def post_session(self, spec: object, outcome: object) -> None:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, object],
        ctx: object,
    ) -> FeatureActionResult:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return FeatureActionResult(ok=False)  # pragma: no cover

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return SidecarStatus(ok=False)  # pragma: no cover

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return NormalizedDevice(  # pragma: no cover
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
        await asyncio.sleep(ADAPTER_HOOK_TIMEOUT_SECONDS + 10)
        return HardwareTelemetry(supported=False)  # pragma: no cover


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

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, object],
        ctx: object,
    ) -> FeatureActionResult:
        raise RuntimeError("feature_action exploded")

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        raise RuntimeError("sidecar_lifecycle exploded")

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

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, object],
        ctx: object,
    ) -> FeatureActionResult:
        return {"wrong": "type"}  # type: ignore[return-value]

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        return ["bad", "value"]  # type: ignore[return-value]

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
    results = await dispatch_discover(adapter, _DiscoveryCtx())  # type: ignore[arg-type]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0].identity_value == "emulator-5554"


@pytest.mark.asyncio
async def test_dispatch_doctor_returns_list() -> None:
    adapter = _GoodAdapter()
    results = await dispatch_doctor(adapter, _DoctorCtx())  # type: ignore[arg-type]
    assert isinstance(results, list)
    assert results[0].check_id == "adb_version"
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_dispatch_health_check_returns_list() -> None:
    adapter = _GoodAdapter()
    results = await dispatch_health_check(adapter, _HealthCtx())  # type: ignore[arg-type]
    assert isinstance(results, list)
    assert results[0].check_id == "device_online"
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_dispatch_lifecycle_action_returns_result() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_lifecycle_action(adapter, "reconnect", {}, _LifecycleCtx())  # type: ignore[arg-type]
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
    result = await dispatch_pre_session(adapter, spec)  # type: ignore[arg-type]
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
    result = await dispatch_post_session(adapter, spec, outcome)  # type: ignore[arg-type]
    assert result is None


# ---------------------------------------------------------------------------
# Timeout tests  (patch the timeout constant so tests don't actually wait 30s)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_discover_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_discover(adapter, _DiscoveryCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "discover"
    assert exc_info.value.pack_id == "vendor-slow"


@pytest.mark.asyncio
async def test_dispatch_doctor_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_doctor(adapter, _DoctorCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "doctor"


@pytest.mark.asyncio
async def test_dispatch_health_check_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_health_check(adapter, _HealthCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "health_check"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_action_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_lifecycle_action(adapter, "state", {}, _LifecycleCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "lifecycle_action"


@pytest.mark.asyncio
async def test_dispatch_pre_session_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    spec = SessionSpec(
        pack_id="vendor-slow",
        platform_id="android_mobile",
        device_identity_value="emulator-5554",
    )
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_pre_session(adapter, spec)  # type: ignore[arg-type]
    assert exc_info.value.hook == "pre_session"


@pytest.mark.asyncio
async def test_dispatch_post_session_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    spec = SessionSpec(
        pack_id="vendor-slow",
        platform_id="android_mobile",
        device_identity_value="emulator-5554",
    )
    outcome = SessionOutcome(ok=True)
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_post_session(adapter, spec, outcome)  # type: ignore[arg-type]
    assert exc_info.value.hook == "post_session"


# ---------------------------------------------------------------------------
# Execution error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_discover_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_discover(adapter, _DiscoveryCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "discover"
    assert "discover exploded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_doctor_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_doctor(adapter, _DoctorCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "doctor"


@pytest.mark.asyncio
async def test_dispatch_health_check_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_health_check(adapter, _HealthCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "health_check"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_action_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_lifecycle_action(adapter, "reconnect", {}, _LifecycleCtx())  # type: ignore[arg-type]
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
        await dispatch_pre_session(adapter, spec)  # type: ignore[arg-type]
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
        await dispatch_post_session(adapter, spec, outcome)  # type: ignore[arg-type]
    assert exc_info.value.hook == "post_session"


# ---------------------------------------------------------------------------
# Contract error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_discover_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_discover(adapter, _DiscoveryCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "discover"
    assert "list" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_doctor_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_doctor(adapter, _DoctorCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "doctor"


@pytest.mark.asyncio
async def test_dispatch_health_check_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_health_check(adapter, _HealthCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "health_check"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_action_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_lifecycle_action(adapter, "state", {}, _LifecycleCtx())  # type: ignore[arg-type]
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
        await dispatch_pre_session(adapter, spec)  # type: ignore[arg-type]
    assert exc_info.value.hook == "pre_session"
    assert "dict" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Error attribute tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_error_attributes() -> None:
    import agent_app.pack.adapter_dispatch as mod

    adapter = _TimeoutAdapter()
    # Use a very short timeout
    original = mod.ADAPTER_HOOK_TIMEOUT_SECONDS
    mod.ADAPTER_HOOK_TIMEOUT_SECONDS = 0.01
    try:
        with pytest.raises(AdapterHookTimeoutError) as exc_info:
            await dispatch_discover(adapter, _DiscoveryCtx())  # type: ignore[arg-type]
        err = exc_info.value
        assert err.pack_id == "vendor-slow"
        assert err.pack_release == "1.0.0"
        assert err.hook == "discover"
    finally:
        mod.ADAPTER_HOOK_TIMEOUT_SECONDS = original


@pytest.mark.asyncio
async def test_execution_error_chained_cause() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_discover(adapter, _DiscoveryCtx())  # type: ignore[arg-type]
    err = exc_info.value
    assert err.__cause__ is not None
    assert isinstance(err.__cause__, RuntimeError)
    assert err.pack_id == "vendor-bad"
    assert err.pack_release == "1.0.0"


# ---------------------------------------------------------------------------
# dispatch_feature_action — happy / timeout / execution-error / contract-error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_feature_action_returns_result() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_feature_action(
        adapter,
        "tunnel",
        "start",
        {},
        _LifecycleCtx(),  # type: ignore[arg-type]
    )
    assert isinstance(result, FeatureActionResult)
    assert result.ok is True
    assert result.detail == "done"
    assert result.data["feature"] == "tunnel"


@pytest.mark.asyncio
async def test_dispatch_feature_action_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_feature_action(
            adapter,
            "tunnel",
            "start",
            {},
            _LifecycleCtx(),  # type: ignore[arg-type]
        )
    assert exc_info.value.hook == "feature_action"
    assert exc_info.value.pack_id == "vendor-slow"


@pytest.mark.asyncio
async def test_dispatch_feature_action_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_feature_action(
            adapter,
            "tunnel",
            "start",
            {},
            _LifecycleCtx(),  # type: ignore[arg-type]
        )
    assert exc_info.value.hook == "feature_action"
    assert "feature_action exploded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_feature_action_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_feature_action(
            adapter,
            "tunnel",
            "start",
            {},
            _LifecycleCtx(),  # type: ignore[arg-type]
        )
    assert exc_info.value.hook == "feature_action"
    assert "FeatureActionResult" in str(exc_info.value)


# ---------------------------------------------------------------------------
# dispatch_sidecar_lifecycle — happy / timeout / execution-error / contract-error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_sidecar_lifecycle_returns_status() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_sidecar_lifecycle(adapter, "tunnel", "start")
    assert isinstance(result, SidecarStatus)
    assert result.ok is True
    assert result.state == "active"
    assert result.detail == "running"


@pytest.mark.asyncio
async def test_dispatch_sidecar_lifecycle_stop_action() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_sidecar_lifecycle(adapter, "tunnel", "stop")
    assert isinstance(result, SidecarStatus)
    assert result.ok is True


@pytest.mark.asyncio
async def test_dispatch_sidecar_lifecycle_status_action() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_sidecar_lifecycle(adapter, "tunnel", "status")
    assert isinstance(result, SidecarStatus)
    assert result.ok is True


@pytest.mark.asyncio
async def test_dispatch_sidecar_lifecycle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_sidecar_lifecycle(adapter, "tunnel", "start")
    assert exc_info.value.hook == "sidecar_lifecycle"
    assert exc_info.value.pack_id == "vendor-slow"


@pytest.mark.asyncio
async def test_dispatch_sidecar_lifecycle_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_sidecar_lifecycle(adapter, "tunnel", "status")
    assert exc_info.value.hook == "sidecar_lifecycle"
    assert "sidecar_lifecycle exploded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_sidecar_lifecycle_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_sidecar_lifecycle(adapter, "tunnel", "stop")
    assert exc_info.value.hook == "sidecar_lifecycle"
    assert "SidecarStatus" in str(exc_info.value)


# ---------------------------------------------------------------------------
# dispatch_normalize_device / dispatch_telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_normalize_device_returns_device() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_normalize_device(adapter, _NormalizeCtx())  # type: ignore[arg-type]
    assert isinstance(result, NormalizedDevice)
    assert result.identity_value == "avd:Pixel_7"


@pytest.mark.asyncio
async def test_dispatch_telemetry_returns_telemetry() -> None:
    adapter = _GoodAdapter()
    result = await dispatch_telemetry(adapter, _TelemetryCtx())  # type: ignore[arg-type]
    assert isinstance(result, HardwareTelemetry)
    assert result.battery_level_percent == 85


@pytest.mark.asyncio
async def test_dispatch_normalize_device_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_normalize_device(adapter, _NormalizeCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "normalize_device"


@pytest.mark.asyncio
async def test_dispatch_telemetry_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_app.pack.adapter_dispatch as mod

    monkeypatch.setattr(mod, "ADAPTER_HOOK_TIMEOUT_SECONDS", 0.01)
    adapter = _TimeoutAdapter()
    with pytest.raises(AdapterHookTimeoutError) as exc_info:
        await dispatch_telemetry(adapter, _TelemetryCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "telemetry"


@pytest.mark.asyncio
async def test_dispatch_normalize_device_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_normalize_device(adapter, _NormalizeCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "normalize_device"


@pytest.mark.asyncio
async def test_dispatch_telemetry_execution_error() -> None:
    adapter = _RaisingAdapter()
    with pytest.raises(AdapterHookExecutionError) as exc_info:
        await dispatch_telemetry(adapter, _TelemetryCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "telemetry"


@pytest.mark.asyncio
async def test_dispatch_normalize_device_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_normalize_device(adapter, _NormalizeCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "normalize_device"
    assert "NormalizedDevice" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_telemetry_contract_error() -> None:
    adapter = _WrongTypeAdapter()
    with pytest.raises(AdapterContractError) as exc_info:
        await dispatch_telemetry(adapter, _TelemetryCtx())  # type: ignore[arg-type]
    assert exc_info.value.hook == "telemetry"
    assert "HardwareTelemetry" in str(exc_info.value)
