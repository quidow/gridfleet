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

    A boot lifecycle action that succeeds but has not yet resolved a device serial
    (emulator still booting, or adb transiently unresponsive) must not proceed to
    spawn Appium: the unresolved connection target would be baked into
    ``--default-capabilities`` as ``appium:udid``, and every session create would
    fail with ``Device <udid> was not in the list of connected devices``. The
    node-state loop catches this and retries next tick, so no ``start_failure`` is
    recorded and the recovery flow does not escalate to review/backoff.
    """
