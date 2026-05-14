from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from sqlalchemy import or_

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement

    from app.devices.models import Device


def looks_like_ip_address(value: str | None) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def parse_ip_from_connection_target(connection_target: str | None) -> str | None:
    if not connection_target:
        return None
    if ":" in connection_target:
        host, _, _port = connection_target.rpartition(":")
        if looks_like_ip_address(host):
            return host
    if looks_like_ip_address(connection_target):
        return connection_target
    return None


def looks_like_ip_port_target(value: str | None) -> bool:
    if not value or ":" not in value:
        return False
    host, _, port = value.rpartition(":")
    return looks_like_ip_address(host) and port.isdigit()


def is_host_scoped_identity(*, identity_scope: str | None) -> bool:
    return identity_scope == "host"


def host_scoped_clause(model: type[Device]) -> ColumnElement[bool]:
    return model.identity_scope == "host"


def non_host_scoped_clause(model: type[Device]) -> ColumnElement[bool]:
    return or_(
        model.identity_scope.is_(None),
        model.identity_scope != "host",
    )


def appium_connection_target(device: Device) -> str:
    connection_target = getattr(device, "connection_target", None)
    if isinstance(connection_target, str) and connection_target:
        return connection_target
    identity_value = getattr(device, "identity_value", None)
    if isinstance(identity_value, str) and identity_value:
        return identity_value
    raise ValueError("Device has no connection target or identity value")


def derive_pack_identity(
    *,
    identity_scheme: str,
    identity_scope: str,
    identity_value: str | None,
    connection_target: str | None,
    ip_address: str | None,
) -> tuple[str, str, str, str | None, str | None]:
    """Derive the canonical pack-shaped identity tuple for a device.

    Returns ``(identity_scheme, identity_scope, identity_value,
    connection_target, ip_address)`` with all resolved values filled in.
    """
    resolved_value = (identity_value or connection_target or ip_address or "").strip()
    if not resolved_value:
        resolved_value = ""
    resolved_target = connection_target or ip_address or resolved_value
    return identity_scheme, identity_scope, resolved_value, resolved_target, ip_address
