from __future__ import annotations

from dataclasses import dataclass

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from agent_app.pack.runtime import RuntimeSpec
from agent_app.pack.runtime_types import AppiumInstallable, RuntimePolicy

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


def resolve_runtime_spec(
    *,
    pack_id: str,
    appium_server: AppiumInstallable,
    appium_driver: AppiumInstallable,
    policy: RuntimePolicy,
) -> RuntimePolicyResolution:
    del pack_id
    if policy.strategy != "recommended":
        return RuntimePolicyResolution(runtime_spec=None, error=f"runtime_strategy_unsupported:{policy.strategy}")

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
            node_major=None,
        ),
        error=None,
    )
