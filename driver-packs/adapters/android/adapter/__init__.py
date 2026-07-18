"""Android (ADB) driver pack adapter."""

from __future__ import annotations

import shutil
from typing import Any, Literal

from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DiscoveryContext,
    DoctorCheckResult,
    DoctorContext,
    HardwareTelemetry,
    HealthCheckResult,
    HealthContext,
    LifecycleActionResult,
    LifecycleContext,
    NormalizedDevice,
    NormalizeDeviceContext,
    SessionOutcome,
    SessionSpec,
    SubprocessEnvContribution,
    TelemetryContext,
)


class Adapter:
    pack_id: str = ""
    pack_release: str = ""
    discovery_scope: str = "pack"

    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
        from .discovery import discover_adb_devices

        return await discover_adb_devices(ctx)

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        from .tools import find_adb, find_android_home

        adb = find_adb()
        adb_ok = shutil.which(adb) is not None or adb != "adb"
        home = find_android_home()
        return [
            DoctorCheckResult(check_id="adb", ok=adb_ok, message="" if adb_ok else "adb not found"),
            DoctorCheckResult(
                check_id="android_home", ok=home is not None, message="" if home else "ANDROID_HOME not set"
            ),
        ]

    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]:
        from .health import health_check

        return await health_check(ctx)

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "release_forwarded_ports", "resolve"],
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult:
        from .lifecycle import lifecycle_action

        return await lifecycle_action(action_id, args, ctx)

    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]:
        from .session import pre_session

        return await pre_session(spec)

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        from .session import post_session

        await post_session(spec, outcome)

    async def normalize_device(self, ctx: NormalizeDeviceContext) -> NormalizedDevice:
        from .normalize import normalize_device

        return await normalize_device(ctx)

    async def telemetry(self, ctx: TelemetryContext) -> HardwareTelemetry:
        from .telemetry import collect_telemetry

        return await collect_telemetry(ctx)

    def tool_versions(self) -> dict[str, str | None]:
        import re
        import subprocess

        from .tools import find_adb

        versions: dict[str, str | None] = {"adb": None, "java": None}

        adb = find_adb()
        try:
            result = subprocess.run(
                [adb, "--version"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
            if match:
                versions["adb"] = match.group(1)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass  # version stays None from default

        try:
            result = subprocess.run(
                ["java", "-version"], capture_output=True, text=True, timeout=5
            )
            combined = result.stdout + result.stderr
            match = re.search(r'"(\d+\.\d+\.\d+)', combined)
            if match:
                versions["java"] = match.group(1)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass  # version stays None from default

        return versions

    def subprocess_env(self) -> SubprocessEnvContribution:
        import os

        from .tools import find_adb, find_android_home

        extra_path_dirs: list[str] = []
        env_vars: dict[str, str] = {}

        adb_path = find_adb()
        adb_dir = os.path.dirname(adb_path)
        if adb_dir and adb_path != "adb":
            extra_path_dirs.append(adb_dir)

        home = find_android_home()
        if home:
            env_vars["ANDROID_HOME"] = home
            env_vars["ANDROID_SDK_ROOT"] = home

        return SubprocessEnvContribution(env_vars=env_vars, extra_path_dirs=extra_path_dirs)
