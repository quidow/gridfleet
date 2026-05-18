"""SQLAlchemy attribute-event guardrail for protected state columns.

Real bugs in this codebase write authoritative columns (``Device.operational_state``,
``AppiumNode.desired_state``, etc.) from callers that bypass the sanctioned writer
modules. The contract is documented in ``CLAUDE.md``; this module enforces it at
runtime by raising ``StateWriteOutsideSanctionedWriterError`` whenever a write
originates outside the allowlist.

Tests that seed state via fixtures use the ``bypass()`` context manager. Production
code never calls ``bypass``.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from sqlalchemy import event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_guard_disabled: ContextVar[bool] = ContextVar("state_write_guard_disabled", default=False)


ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    ("devices", "operational_state"): frozenset(
        {
            "app.devices.services.state",
            "app.devices.services.lifecycle_state_machine",
        }
    ),
    ("devices", "hold"): frozenset(
        {
            "app.devices.services.state",
            "app.devices.services.lifecycle_state_machine",
        }
    ),
    ("devices", "lifecycle_policy_state"): frozenset({"app.devices.services.lifecycle_policy_state"}),
    ("appium_nodes", "desired_state"): frozenset({"app.appium_nodes.services.desired_state_writer"}),
    ("appium_nodes", "desired_port"): frozenset({"app.appium_nodes.services.desired_state_writer"}),
    ("appium_nodes", "transition_token"): frozenset({"app.appium_nodes.services.desired_state_writer"}),
    ("appium_nodes", "transition_deadline"): frozenset({"app.appium_nodes.services.desired_state_writer"}),
    ("appium_nodes", "pid"): frozenset(
        {
            "app.appium_nodes.services.reconciler_agent",
            "app.appium_nodes.services.reconciler",
            "app.appium_nodes.services.heartbeat",
        }
    ),
    ("appium_nodes", "port"): frozenset(
        {
            "app.appium_nodes.services.reconciler_agent",
            "app.appium_nodes.services.reconciler",
        }
    ),
    ("appium_nodes", "active_connection_target"): frozenset(
        {
            "app.appium_nodes.services.reconciler_agent",
            "app.devices.services.capability",
            "app.devices.services.verification_execution",
        }
    ),
    ("appium_nodes", "health_running"): frozenset(
        {
            "app.devices.services.health",
            "app.appium_nodes.services.heartbeat",
        }
    ),
    ("appium_nodes", "health_state"): frozenset(
        {
            "app.devices.services.health",
            "app.appium_nodes.services.heartbeat",
        }
    ),
    ("appium_nodes", "last_health_checked_at"): frozenset({"app.devices.services.health"}),
    ("appium_nodes", "last_observed_at"): frozenset({"app.appium_nodes.services.heartbeat"}),
}


class StateWriteOutsideSanctionedWriterError(RuntimeError):
    """Raised when a protected state column is written from outside its allowlist."""


@contextmanager
def bypass() -> Iterator[None]:
    """Disable the guardrail within the scope. Test fixtures only."""
    token = _guard_disabled.set(True)
    try:
        yield
    finally:
        _guard_disabled.reset(token)


def _calling_module() -> str:
    # Frame walk from _calling_module to the site that wrote the attribute:
    #   0: _calling_module (this function)
    #   1: _listener closure
    #   2: sqlalchemy.orm.events.wrap  (SA dispatch shim)
    #   3: sqlalchemy.orm.attributes.fire_replace_event
    #   4: sqlalchemy.orm.attributes.set
    #   5: sqlalchemy.orm.attributes.__set__  (the descriptor)
    #   6: actual caller (the module we want to report)
    #
    # If the exact SQLAlchemy call chain differs (e.g. during ORM init), we
    # scan up until we find the first non-sqlalchemy frame beyond frame 1.
    frame = inspect.currentframe()
    # skip frames 0 and 1 unconditionally
    for _ in range(2):
        if frame is None:
            return "<unknown>"
        frame = frame.f_back
    # scan past any remaining SQLAlchemy internal frames
    while frame is not None:
        module = inspect.getmodule(frame)
        name = module.__name__ if module is not None else ""
        if not name.startswith("sqlalchemy"):
            return name if name else "<unknown>"
        frame = frame.f_back
    return "<unknown>"


def _make_listener(table: str, column: str, allowlist: frozenset[str]) -> Callable[..., Any]:
    def _listener(target: object, value: object, oldvalue: object, initiator: object) -> object:
        if _guard_disabled.get():
            return value
        caller = _calling_module()
        if caller in allowlist:
            return value
        raise StateWriteOutsideSanctionedWriterError(
            f"{table}.{column} written from {caller!r}; allowed writers: {sorted(allowlist)}"
        )

    return _listener


_registered = False


def register() -> None:
    """Wire the listeners onto each protected mapped attribute.

    Idempotent: safe to call multiple times across tests. Tests may import this
    module before the FastAPI lifespan runs, so registration is also triggered
    lazily from ``backend/tests/conftest.py``.
    """
    global _registered
    if _registered:
        return
    from app.appium_nodes.models import AppiumNode  # noqa: PLC0415
    from app.devices.models import Device  # noqa: PLC0415

    model_by_table: dict[str, type[object]] = {
        "devices": Device,
        "appium_nodes": AppiumNode,
    }
    for (table, column), allowlist in ALLOWLIST.items():
        attr = getattr(model_by_table[table], column)
        event.listen(attr, "set", _make_listener(table, column, allowlist), retval=True)
    _registered = True
