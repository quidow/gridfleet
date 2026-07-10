"""Cross-setting invariants checked on writes and at scheduler boot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.appium_nodes.services.host_sweep import HOST_SWEEP_INTERVAL_SEC

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.type_defs import SettingValue


def _as_num(value: SettingValue) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"cross-invariant key is not numeric: {value!r}")
    return float(value)


def cross_invariant_errors(get: Callable[[str], SettingValue]) -> list[str]:
    """Return one message per violated cross-setting invariant."""
    errors: list[str] = []
    offline_after = _as_num(get("general.host_offline_after_sec"))
    if offline_after <= HOST_SWEEP_INTERVAL_SEC:
        errors.append(
            f"general.host_offline_after_sec ({offline_after:g}) must exceed the host "
            f"sweep tick ({HOST_SWEEP_INTERVAL_SEC:g}s)"
        )
    idle = _as_num(get("grid.session_idle_timeout_sec"))
    ceiling = _as_num(get("grid.session_idle_timeout_ceiling_sec"))
    if idle > ceiling:
        errors.append(
            f"grid.session_idle_timeout_sec ({idle:g}) must not exceed "
            f"grid.session_idle_timeout_ceiling_sec ({ceiling:g})"
        )
    grace = _as_num(get("grid.session_first_command_grace_sec"))
    if grace >= idle:
        errors.append(
            f"grid.session_first_command_grace_sec ({grace:g}) must be below grid.session_idle_timeout_sec ({idle:g})"
        )
    return errors
