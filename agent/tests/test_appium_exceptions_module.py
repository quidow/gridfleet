"""Exceptions live in agent_app.appium.exceptions."""

from __future__ import annotations


def test_exceptions_importable_from_dedicated_module() -> None:
    from agent_app.appium.exceptions import (
        AlreadyRunningError,
        DeviceNotFoundError,
        InvalidStartPayloadError,
        PortOccupiedError,
        RuntimeMissingError,
        RuntimeNotInstalledError,
        StartupTimeoutError,
    )

    for cls in (
        AlreadyRunningError,
        DeviceNotFoundError,
        InvalidStartPayloadError,
        PortOccupiedError,
        RuntimeMissingError,
        RuntimeNotInstalledError,
        StartupTimeoutError,
    ):
        assert issubclass(cls, RuntimeError)


def test_exception_classes_are_defined_in_exceptions_module() -> None:
    from agent_app.appium.exceptions import (
        AlreadyRunningError,
        DeviceNotFoundError,
        InvalidStartPayloadError,
        PortOccupiedError,
        RuntimeMissingError,
        RuntimeNotInstalledError,
        StartupTimeoutError,
    )

    for cls in (
        AlreadyRunningError,
        DeviceNotFoundError,
        InvalidStartPayloadError,
        PortOccupiedError,
        RuntimeMissingError,
        RuntimeNotInstalledError,
        StartupTimeoutError,
    ):
        assert cls.__module__ == "agent_app.appium.exceptions", (
            f"{cls.__name__} is defined in {cls.__module__}, not agent_app.appium.exceptions"
        )
