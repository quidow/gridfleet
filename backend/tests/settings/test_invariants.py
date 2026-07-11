from __future__ import annotations

from typing import TYPE_CHECKING

from app.settings.invariants import cross_invariant_errors
from app.settings.registry import SETTINGS_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.type_defs import SettingValue


def _getter(overrides: dict[str, SettingValue]) -> Callable[[str], SettingValue]:
    defaults = {key: definition.default for key, definition in SETTINGS_REGISTRY.items()}
    merged = {**defaults, **overrides}
    return lambda key: merged[key]


def test_registry_defaults_satisfy_cross_invariants() -> None:
    assert cross_invariant_errors(_getter({})) == []


def test_offline_threshold_must_exceed_sweep_tick() -> None:
    errors = cross_invariant_errors(_getter({"general.host_offline_after_sec": 15}))
    assert len(errors) == 1
    assert "general.host_offline_after_sec" in errors[0]
    assert "sweep tick" in errors[0]


def test_idle_timeout_must_not_exceed_ceiling() -> None:
    errors = cross_invariant_errors(
        _getter({"grid.session_idle_timeout_sec": 7200, "grid.session_idle_timeout_ceiling_sec": 3600})
    )
    assert len(errors) == 1
    assert "grid.session_idle_timeout_sec" in errors[0]
    assert "grid.session_idle_timeout_ceiling_sec" in errors[0]


def test_first_command_grace_must_stay_below_idle_timeout() -> None:
    errors = cross_invariant_errors(_getter({"grid.session_idle_timeout_sec": 120}))
    assert len(errors) == 1
    assert "grid.session_first_command_grace_sec" in errors[0]
    assert "grid.session_idle_timeout_sec" in errors[0]


def test_equal_idle_and_ceiling_is_allowed() -> None:
    assert (
        cross_invariant_errors(
            _getter(
                {
                    "grid.session_idle_timeout_sec": 60,
                    "grid.session_idle_timeout_ceiling_sec": 60,
                    "grid.session_first_command_grace_sec": 30,
                }
            )
        )
        == []
    )


def test_queue_timeout_must_exceed_long_poll() -> None:
    errors = cross_invariant_errors(_getter({"grid.queue_timeout_sec": 20}))
    assert any("grid.queue_timeout_sec" in error and "long-poll" in error for error in errors)


def test_queue_timeout_above_long_poll_is_clean() -> None:
    assert cross_invariant_errors(_getter({"grid.queue_timeout_sec": 300})) == []
