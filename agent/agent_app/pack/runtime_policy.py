from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from agent_app.pack.runtime import RuntimeSpec
from agent_app.pack.runtime_types import AppiumInstallable, RuntimePolicy

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["AppiumInstallable", "RuntimePolicy", "RuntimePolicyResolution", "resolve_runtime_spec"]


@dataclass(frozen=True)
class RuntimePolicyResolution:
    runtime_spec: RuntimeSpec | None
    error: str | None


def _valid_version(installable: AppiumInstallable, version: str, field_name: str) -> str | None:
    if version in installable.known_bad:
        return f"pinned_version_unavailable:{field_name}={version} known_bad"
    if Version(version) not in SpecifierSet(installable.version):
        return f"pinned_version_unavailable:{field_name}={version} outside {installable.version}"
    return None


def _recommended_version(installable: AppiumInstallable, field_name: str) -> tuple[str | None, str | None]:
    if not installable.recommended:
        return None, f"pinned_version_unavailable:{field_name}=recommended missing"
    error = _valid_version(installable, installable.recommended, field_name)
    if error:
        return None, error
    return installable.recommended, None


def _latest_patch_version(
    installable: AppiumInstallable,
    *,
    field_name: str,
    available_versions: Mapping[str, Sequence[str]],
) -> tuple[str | None, str | None]:
    if installable.source != "npm":
        return None, f"version_resolution_unavailable:latest_patch_source={installable.source}"
    if not installable.recommended:
        return None, f"pinned_version_unavailable:{field_name}=recommended missing"

    recommended = Version(installable.recommended)
    specifier = SpecifierSet(installable.version)
    candidates: list[Version] = []
    for raw in available_versions.get(installable.package, ()):
        try:
            parsed = Version(raw)
        except InvalidVersion:
            continue
        if parsed.is_prerelease:
            continue
        if parsed.major != recommended.major or parsed.minor != recommended.minor:
            continue
        if parsed not in specifier:
            continue
        if str(parsed) in installable.known_bad:
            continue
        candidates.append(parsed)

    if not candidates:
        return None, f"pinned_version_unavailable:{field_name}=latest_patch no candidates"
    return str(max(candidates)), None


def resolve_runtime_spec(
    *,
    pack_id: str,
    appium_server: AppiumInstallable,
    appium_driver: AppiumInstallable,
    policy: RuntimePolicy,
    available_versions: Mapping[str, Sequence[str]] | None = None,
) -> RuntimePolicyResolution:
    del pack_id
    if policy.strategy == "latest_patch":
        versions = available_versions or {}
        server_version, server_error = _latest_patch_version(
            appium_server,
            field_name="appium_server_version",
            available_versions=versions,
        )
        if server_error:
            return RuntimePolicyResolution(runtime_spec=None, error=server_error)
        driver_version, driver_error = _latest_patch_version(
            appium_driver,
            field_name="appium_driver_version",
            available_versions=versions,
        )
        if driver_error:
            return RuntimePolicyResolution(runtime_spec=None, error=driver_error)
        assert server_version is not None
        assert driver_version is not None

    elif policy.strategy == "exact":
        if not policy.appium_server_version or not policy.appium_driver_version:
            return RuntimePolicyResolution(runtime_spec=None, error="pinned_version_unavailable:exact pins missing")
        server_version = policy.appium_server_version
        driver_version = policy.appium_driver_version
        assert server_version is not None
        assert driver_version is not None
        for installable, version, field_name in (
            (appium_server, server_version, "appium_server_version"),
            (appium_driver, driver_version, "appium_driver_version"),
        ):
            error = _valid_version(installable, version, field_name)
            if error:
                return RuntimePolicyResolution(runtime_spec=None, error=error)
    else:
        recommended_server_version, server_error = _recommended_version(appium_server, "appium_server_version")
        if server_error:
            return RuntimePolicyResolution(runtime_spec=None, error=server_error)
        recommended_driver_version, driver_error = _recommended_version(appium_driver, "appium_driver_version")
        if driver_error:
            return RuntimePolicyResolution(runtime_spec=None, error=driver_error)
        assert recommended_server_version is not None
        assert recommended_driver_version is not None
        server_version = recommended_server_version
        driver_version = recommended_driver_version

    return RuntimePolicyResolution(
        runtime_spec=RuntimeSpec(
            server_package=appium_server.package,
            server_version=server_version,
            drivers=((appium_driver.package, driver_version, appium_driver.source, appium_driver.github_repo),),
            plugins=(),
            node_major=None,
        ),
        error=None,
    )
