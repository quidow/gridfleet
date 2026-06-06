"""Shared SQLAlchemy predicate: an Appium node is viable for new sessions.

Both allocators (the run reservation allocator and the grid new-session
allocator) must reject a device whose Appium node is mid-restart or has no live
process/connection. Factoring the predicate here keeps the two allocators from
drifting: a device that one allocator would refuse the other must refuse too.

The predicate is expressed against ``AppiumNode`` columns and assumes the query
``outerjoin``s ``AppiumNode`` on ``AppiumNode.device_id == Device.id``. A device
with no node row (``AppiumNode.id IS NULL``) is treated as viable — node-less
devices are filtered elsewhere by their operational state / node target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, or_

from app.appium_nodes.models import AppiumNode

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

    from app.devices.models import Device


def node_viable_predicate() -> ColumnElement[bool]:
    """A device's Appium node is up and not transitioning.

    Mirrors the run allocator's node filter: a live ``pid`` and
    ``active_connection_target`` with no in-flight ``transition_token``.
    """
    return or_(
        AppiumNode.id.is_(None),
        and_(
            AppiumNode.pid.is_not(None),
            AppiumNode.active_connection_target.is_not(None),
            AppiumNode.transition_token.is_(None),
        ),
    )


def device_node_is_viable(device: Device) -> bool:
    """Python-side equivalent of :func:`node_viable_predicate` for a loaded device.

    Used by the grid allocator's locked re-check, where the row is already
    eager-loaded and a re-query would be wasteful. A device with no node row is
    viable (matches the ``AppiumNode.id IS NULL`` arm).
    """
    node = device.appium_node
    if node is None:
        return True
    return node.pid is not None and node.active_connection_target is not None and node.transition_token is None
