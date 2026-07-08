from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.packs.models import (
        DriverPackRelease,
        HostPackInstallation,
    )


def desired_driver_version(pack_row: HostPackInstallation, release: DriverPackRelease | None) -> str | None:
    spec = pack_row.resolved_install_spec or {}
    if "appium_driver_version" in spec:
        value = spec["appium_driver_version"]
        return str(value) if value is not None else None
    appium_driver = spec.get("appium_driver")
    if isinstance(appium_driver, dict) and appium_driver:
        value = next(iter(appium_driver.values()))
        return str(value) if value is not None else None
    if release is not None and release.manifest_json:
        recommended = release.manifest_json.get("appium_driver", {}).get("recommended")
        return str(recommended) if recommended is not None else None
    return None


def installed_driver_version(pack_row: HostPackInstallation | None) -> str | None:
    if pack_row is None:
        return None
    driver_specs = pack_row.driver_specs or []
    if not driver_specs:
        return None
    version = driver_specs[0].get("version")
    return str(version) if version is not None else None


def has_driver_drift(pack_row: HostPackInstallation, release: DriverPackRelease | None) -> bool:
    desired = desired_driver_version(pack_row, release)
    installed = installed_driver_version(pack_row)
    return desired is not None and installed is not None and desired != installed
