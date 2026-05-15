"""Public exception classes exported by the devices domain."""

from app.devices.services.identity_conflicts import DeviceIdentityConflictError

__all__ = ["DeviceIdentityConflictError"]
