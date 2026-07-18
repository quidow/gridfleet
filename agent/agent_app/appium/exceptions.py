"""Domain exceptions raised by the Appium process manager."""

from __future__ import annotations


class RuntimeNotInstalledError(RuntimeError):
    """Raised when the requested runtime is not installed on disk."""


class PortOccupiedError(RuntimeError):
    """Raised when the requested port is already in use by another process."""


class AlreadyRunningError(RuntimeError):
    """Raised when an Appium instance is already running for the target."""


class StartupTimeoutError(RuntimeError):
    """Raised when an Appium process fails to become ready within the timeout."""


class RuntimeMissingError(RuntimeError):
    """Raised when no runtime is available to serve the requested pack."""


class InvalidStartPayloadError(RuntimeError):
    """Raised when an /agent/appium/start payload is missing or malformed."""


class DeviceNotFoundError(RuntimeError):
    """Raised when the addressed device or managed port is unknown."""


class StartDeferredError(RuntimeError):
    """Raised when a start must be retried later without recording a failure.

    Used when a start cannot proceed yet but the condition is transient and not
    a device/start failure: the driver-pack adapter or runtime is still loading,
    the requested release is not installed yet, or the release changed mid-start.
    The node-state loop catches this and retries next tick, so no ``start_failure``
    is recorded and the recovery flow does not escalate to review/backoff.
    """
