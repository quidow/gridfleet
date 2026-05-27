"""High-level operator-driven Appium node lifecycle helpers.

All operator-initiated node lifecycle writes (start / stop / restart) must flow
through this module so that the ``device_intents`` table is the single source of
truth for desired ``appium_nodes`` state. Direct ``write_desired_state`` calls
from operator code are forbidden — they leave stale intent payloads that the
intent reconciler keeps re-asserting onto the AppiumNode row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.devices.services.intent import (
    register_intents_and_reconcile,
    revoke_intents_and_reconcile,
)
from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    PRIORITY_OPERATOR_STOP,
    IntentRegistration,
    NodeRunningPrecondition,
)
from app.devices.services.review import clear_review_required

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.protocols import SettingsReader
    from app.devices.models import Device


def operator_start_source(device_id: uuid.UUID) -> str:
    return f"operator:start:{device_id}"


def operator_stop_sources(device_id: uuid.UUID) -> list[str]:
    return [f"operator:stop:node:{device_id}", f"operator:stop:grid:{device_id}"]


def operator_start_precondition(device_id: uuid.UUID) -> NodeRunningPrecondition:
    """Precondition retiring an operator:start intent once the node is observed running.

    ``expected: False`` means "satisfied while the node is NOT running". The
    intent represents an operator's desire to start the node, so once the node
    reaches ``observed_running == True`` the precondition flips and the
    reconciler sweep deletes the row.
    """
    return {
        "kind": "node_running",
        "device_id": str(device_id),
        "expected": False,
    }


def operator_start_intent(device: Device, desired_port: int) -> IntentRegistration:
    return IntentRegistration(
        source=operator_start_source(device.id),
        axis=NODE_PROCESS,
        payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY, "desired_port": desired_port},
        precondition=operator_start_precondition(device.id),
    )


def operator_restart_intent(device: Device, desired_port: int, *, settings: SettingsReader) -> IntentRegistration:
    window_sec = int(settings.get("appium_reconciler.restart_window_sec"))
    deadline = datetime.now(UTC) + timedelta(seconds=window_sec)
    return IntentRegistration(
        source=operator_start_source(device.id),
        axis=NODE_PROCESS,
        payload={
            "action": "start",
            "priority": PRIORITY_AUTO_RECOVERY,
            "desired_port": desired_port,
            "transition_token": str(uuid.uuid4()),
            "transition_deadline": deadline.isoformat(),
        },
        precondition=operator_start_precondition(device.id),
        expires_at=deadline,
    )


def operator_stop_intents(device_id: uuid.UUID) -> list[IntentRegistration]:
    return [
        IntentRegistration(
            source=f"operator:stop:node:{device_id}",
            axis=NODE_PROCESS,
            payload={"action": "stop", "priority": PRIORITY_OPERATOR_STOP, "stop_mode": "hard"},
        ),
        IntentRegistration(
            source=f"operator:stop:grid:{device_id}",
            axis=GRID_ROUTING,
            payload={"accepting_new_sessions": False, "priority": PRIORITY_OPERATOR_STOP},
        ),
    ]


async def request_start(
    db: AsyncSession,
    device: Device,
    *,
    caller: DesiredStateCaller,
    reason: str,
    settings: SettingsReader,
) -> AppiumNode:
    """Register an operator:start intent and return the (existing or newly-created)
    AppiumNode row. The intent reconciler runs synchronously inside
    register_intents_and_reconcile, so the AppiumNode's desired_state/desired_port
    are up to date on return.
    """
    if device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned")

    desired_port = (await candidate_ports(db, host_id=device.host_id, settings=settings))[0]

    node: AppiumNode | None = device.appium_node
    if node is None:
        node = AppiumNode(
            device_id=device.id,
            port=desired_port,
            grid_url=settings.get("grid.hub_url"),
        )
        db.add(node)
        await db.flush()
        device.appium_node = node

    await revoke_intents_and_reconcile(
        db,
        device_id=device.id,
        sources=operator_stop_sources(device.id),
        reason=reason,
    )
    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=[operator_start_intent(device, desired_port)],
        reason=reason,
    )
    if caller in {"operator_route", "operator_restart"}:
        await clear_review_required(db, device, reason="Operator started Appium node", source="start_node")
    await db.refresh(node)
    return node


async def request_stop(
    db: AsyncSession,
    device: Device,
    *,
    caller: DesiredStateCaller,
    reason: str,
) -> AppiumNode:
    """Register operator:stop intents (node + grid). Returns the node row for the
    convenience of route handlers; the caller column ``caller`` is accepted for
    symmetry with request_start/request_restart and future audit-logging use.

    Invariant — callers must gate ``observed_running``: this helper only checks
    that an ``AppiumNode`` row exists. Wrappers in ``reconciler_agent.stop_node``
    and ``bulk._bulk_stop_one`` enforce ``observed_running`` upfront and raise
    ``NodeManagerError("No running node for device …")`` with the
    operator-facing error message. Registering operator:stop intents against an
    already-stopped node is otherwise idempotent (the intent reconciler maps to
    ``desired_state="stopped"`` either way).
    """
    del caller  # currently unused — kept for parity with request_start/request_restart
    node: AppiumNode | None = device.appium_node
    if node is None:
        raise NodeManagerError(f"No node row for device {device.id}")

    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=operator_stop_intents(device.id),
        reason=reason,
    )
    await db.refresh(node)
    return node


async def request_restart(
    db: AsyncSession,
    device: Device,
    *,
    caller: DesiredStateCaller,
    reason: str,
    settings: SettingsReader,
) -> AppiumNode:
    """Register an operator:start intent in restart form (with fresh
    transition_token + expires_at). If the node isn't currently observed running,
    fall back to request_start (no token, no deadline) — mirrors the existing
    bulk._bulk_restart_one fallback.
    """
    node: AppiumNode | None = device.appium_node
    if node is None or not node.observed_running:
        return await request_start(db, device, caller=caller, reason=reason, settings=settings)

    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=[operator_restart_intent(device, node.port, settings=settings)],
        reason=reason,
    )
    if caller == "operator_restart":
        await clear_review_required(db, device, reason="Operator restarted Appium node", source="restart_node")
    await db.refresh(node)
    return node
