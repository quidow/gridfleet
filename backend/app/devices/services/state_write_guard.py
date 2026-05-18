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
    from collections.abc import Callable, Iterable, Iterator

_guard_disabled: ContextVar[bool] = ContextVar("state_write_guard_disabled", default=False)


ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    ("devices", "operational_state"): frozenset(
        {
            "app.devices.services.state",
            "app.devices.services.lifecycle_state_machine",
            # Device creation paths: initial state is set at construction time, not via the
            # state machine (device does not exist yet so there is no prior state to transition).
            "app.devices.services.write",
            "app.seeding.factories.device",
            # Seeding scenarios write operational_state directly to create demo/test fixtures.
            "app.seeding.scenarios.minimal",
            "app.seeding.scenarios.chaos",
            "app.seeding.scenarios.full_demo",
        }
    ),
    ("devices", "hold"): frozenset(
        {
            "app.devices.services.state",
            "app.devices.services.lifecycle_state_machine",
            # Seeding factories and demo scenarios set hold at construction/seed time.
            "app.seeding.factories.device",
            "app.seeding.scenarios.full_demo",
        }
    ),
    ("devices", "lifecycle_policy_state"): frozenset(
        {
            "app.devices.services.lifecycle_policy_state",
            # Demo seeding writes lifecycle_policy_state directly for realistic fixture data.
            "app.seeding.scenarios.full_demo",
        }
    ),
    ("appium_nodes", "desired_state"): frozenset(
        {
            "app.appium_nodes.services.desired_state_writer",
            # Demo seeding creates fully-formed AppiumNode rows with desired_state set.
            "app.seeding.scenarios.full_demo",
        }
    ),
    ("appium_nodes", "desired_port"): frozenset(
        {
            "app.appium_nodes.services.desired_state_writer",
            # Demo seeding creates fully-formed AppiumNode rows with desired_port set.
            "app.seeding.scenarios.full_demo",
        }
    ),
    ("appium_nodes", "transition_token"): frozenset(
        {
            "app.appium_nodes.services.desired_state_writer",
            "app.appium_nodes.services.reconciler_agent",
            "app.appium_nodes.routers.admin",
        }
    ),
    ("appium_nodes", "transition_deadline"): frozenset(
        {
            "app.appium_nodes.services.desired_state_writer",
            "app.appium_nodes.services.reconciler_agent",
            "app.appium_nodes.routers.admin",
        }
    ),
    ("appium_nodes", "pid"): frozenset(
        {
            "app.appium_nodes.services.reconciler_agent",
            "app.appium_nodes.services.reconciler",
            "app.appium_nodes.services.heartbeat",
            # Demo seeding creates fully-formed AppiumNode rows with pid set.
            "app.seeding.scenarios.full_demo",
            # Verification teardown clears pid to signal the node has stopped.
            "app.devices.services.verification_execution",
        }
    ),
    ("appium_nodes", "port"): frozenset(
        {
            "app.appium_nodes.services.reconciler_agent",
            "app.appium_nodes.services.reconciler",
            # Node creation paths: port is set at construction time when the AppiumNode row
            # does not yet exist.  These modules create the row; subsequent port changes go
            # through the reconciler.
            "app.devices.services.bulk",
            "app.devices.services.lifecycle_policy",
            "app.seeding.scenarios.full_demo",
        }
    ),
    ("appium_nodes", "active_connection_target"): frozenset(
        {
            "app.appium_nodes.services.reconciler_agent",
            "app.devices.services.capability",
            "app.devices.services.verification_execution",
            # Demo seeding creates fully-formed AppiumNode rows with active_connection_target set.
            "app.seeding.scenarios.full_demo",
        }
    ),
    ("appium_nodes", "health_running"): frozenset(
        {
            "app.devices.services.health",
            "app.appium_nodes.services.heartbeat",
            # Reconciler agent clears health_running when stale health state is detected.
            "app.appium_nodes.services.reconciler_agent",
        }
    ),
    ("appium_nodes", "health_state"): frozenset(
        {
            "app.devices.services.health",
            "app.appium_nodes.services.heartbeat",
            # Reconciler agent clears health_state when stale health state is detected.
            "app.appium_nodes.services.reconciler_agent",
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


def _resolve_caller_name(name_iter: Iterable[str | None]) -> str:
    """Pick the first real application/test module name from a frame-name sequence.

    ``None`` represents an unresolvable frame (``<string>`` exec, etc.) and is
    skipped. Names whose ``str.startswith("sqlalchemy")`` returns ``True`` are
    SQLAlchemy internals and skipped. Returns the first remaining name or
    ``"<unknown>"`` if the iterable is exhausted.
    """
    for name in name_iter:
        if name is None:
            continue
        if name.startswith("sqlalchemy"):
            continue
        return name
    return "<unknown>"


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
    # When the write originates from a constructor kwarg (e.g.
    # ``Device(operational_state=...)``), SA's declarative metaclass generates
    # ``_declarative_constructor`` via ``exec`` into a ``<string>`` code object.
    # ``inspect.getmodule`` returns ``None`` for those frames; the resolver
    # skips them so the walk continues to the real caller.
    frame = inspect.currentframe()
    for _ in range(2):
        if frame is None:
            return "<unknown>"
        frame = frame.f_back

    def _names() -> Iterator[str | None]:
        f = frame
        while f is not None:
            module = inspect.getmodule(f)
            yield module.__name__ if module is not None else None
            f = f.f_back

    return _resolve_caller_name(_names())


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

    model_by_table: dict[str, type[Any]] = {
        "devices": Device,
        "appium_nodes": AppiumNode,
    }
    for (table, column), allowlist in ALLOWLIST.items():
        attr = getattr(model_by_table[table], column)
        event.listen(attr, "set", _make_listener(table, column, allowlist), retval=True)
    _registered = True
