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

from sqlalchemy import or_, select

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.devices.models import DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    PRIORITY_OPERATOR_STOP,
    RECOVERY,
    IntentRegistration,
    NodeRunningPrecondition,
)
from app.devices.services.lifecycle_policy_state import clear_operator_start_suppression
from app.devices.services.observation_reason import ObservationReason

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.protocols import SettingsReader
    from app.devices.models import Device
    from app.devices.protocols import ReviewProtocol
    from app.events.protocols import EventPublisher


def operator_start_source(device_id: uuid.UUID) -> str:
    return f"operator:start:{device_id}"


def operator_stop_sources(device_id: uuid.UUID) -> list[str]:
    return [
        f"operator:stop:node:{device_id}",
        f"operator:stop:grid:{device_id}",
        f"operator:stop:recovery:{device_id}",
    ]


async def operator_stop_active(db: AsyncSession, device_id: uuid.UUID) -> bool:
    """True while a sticky operator:stop is in force for the device.

    Keys on the canonical node-process stop source (operator stop registers it with
    no TTL). Callers that would otherwise revoke ``operator_stop_sources`` for a
    non-operator-start reason — e.g. a re-verify, whose node-start path runs through
    ``request_start`` and clears the stop — use this to refuse instead, so an
    operator-stopped device is never silently revived (N13b).
    """
    now = datetime.now(UTC)
    found = await db.scalar(
        select(DeviceIntent.id)
        .where(
            DeviceIntent.device_id == device_id,
            DeviceIntent.source == f"operator:stop:node:{device_id}",
            or_(DeviceIntent.expires_at.is_(None), DeviceIntent.expires_at > now),
        )
        .limit(1)
    )
    return found is not None


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
    window_sec = settings.get_int("appium_reconciler.restart_window_sec")
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
        # An operator stop is sticky: deny auto-recovery so the device_connectivity
        # loop suppresses recovery (recovery_allowed=False) instead of spinning a
        # doomed prio-20 start it can never make win against this stop (N13). The
        # operator-start path revokes this via operator_stop_sources.
        IntentRegistration(
            source=f"operator:stop:recovery:{device_id}",
            axis=RECOVERY,
            payload={"allowed": False, "priority": PRIORITY_OPERATOR_STOP, "reason": "Operator stopped the node"},
        ),
    ]


class OperatorNodeLifecycleService:
    def __init__(self, *, settings: SettingsReader, publisher: EventPublisher, review: ReviewProtocol) -> None:
        self._settings = settings
        self._publisher = publisher
        self._review = review

    async def request_start(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode:
        """Register an operator:start intent and return the (existing or newly-created)
        AppiumNode row. The intent reconciler runs synchronously inside
        register_intents_and_reconcile, so the AppiumNode's desired_state/desired_port
        are up to date on return.
        """
        if device.host_id is None:
            raise NodeManagerError(f"Device {device.id} has no host assigned")

        node: AppiumNode | None = device.appium_node
        if node is None:
            # First-time allocation: no node row yet. candidate_ports()[0] picks
            # the lowest free port for the host.
            desired_port = (await candidate_ports(db, host_id=device.host_id, settings=self._settings))[0]
            node = AppiumNode(
                device_id=device.id,
                port=desired_port,
            )
            db.add(node)
            await db.flush()
            device.appium_node = node
        else:
            # Existing node: pin its current port. candidate_ports re-offers the
            # node's own port as "free" during the pid-NULL gap (e.g. after a
            # kill -9 while the agent death-watcher respawns on the NEXT free
            # port), so taking candidate_ports()[0] reallocates the node onto a
            # different port and induces a two-supervisor oscillation. Pinning
            # node.port keeps desired_port stable across the gap.
            desired_port = node.port

        revoke_sources = list(operator_stop_sources(device.id))
        if caller in {"operator_route", "operator_restart"}:
            # An explicit operator start overrides any failure state. Clear the
            # crash/connectivity stop intents too — a leftover health_failure:node
            # stop (priority 60) would otherwise outrank the operator start
            # (priority 20) and silently block it.
            revoke_sources += [
                f"health_failure:node:{device.id}",
                f"health_failure:recovery:{device.id}",
                f"connectivity:{device.id}",
            ]
        await IntentService(db).revoke_intents_and_reconcile(
            device_id=device.id,
            sources=revoke_sources,
            reason=reason,
            publisher=self._publisher,
        )
        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[operator_start_intent(device, desired_port)],
            reason=reason,
            publisher=self._publisher,
        )
        if caller in {"operator_route", "operator_restart"}:
            # An explicit operator start overrides any prior recovery suppression.
            # request_start already revoked the operator:stop deny intents above;
            # clear the matching JSON residue so the device stops deriving
            # recovery_state="suppressed" (presenter "blocked" / "Recovery Paused")
            # while it is actually running and available.
            clear_operator_start_suppression(device)
            await self._review.clear_review_required(
                db, device, reason="Operator started Appium node", source="start_node"
            )
        await db.refresh(node)
        return node

    async def request_stop(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode:
        """Register operator:stop intents (node + grid). Returns the node row for the
        convenience of route handlers; ``caller`` distinguishes genuine operator stops
        (which record an audit row) from verification-driven stops (which do not).

        Invariant — callers must gate ``observed_running``: this helper only checks
        that an ``AppiumNode`` row exists. Wrappers in ``reconciler_agent.stop_node``
        and ``bulk._bulk_stop_one`` enforce ``observed_running`` upfront and raise
        ``NodeManagerError("No running node for device …")`` with the
        operator-facing error message. Registering operator:stop intents against an
        already-stopped node is otherwise idempotent (the intent reconciler maps to
        ``desired_state="stopped"`` either way).
        """
        node: AppiumNode | None = device.appium_node
        if node is None:
            raise NodeManagerError(f"No node row for device {device.id}")

        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=operator_stop_intents(device.id),
            reason=reason,
            publisher=self._publisher,
            # Verification-driven stops carry their own reason via the verification
            # flow; only label genuine operator calls (same caller set request_start
            # uses for its operator-override branches).
            observed_reason=(
                ObservationReason.operator_stopped if caller in {"operator_route", "operator_restart"} else None
            ),
        )
        await db.refresh(node)
        return node

    async def request_restart(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode:
        """Register an operator:start intent in restart form (with fresh
        transition_token + expires_at). If the node isn't currently observed running,
        fall back to request_start (no token, no deadline) — mirrors the existing
        bulk._bulk_restart_one fallback.
        """
        node: AppiumNode | None = device.appium_node
        if node is None or not node.observed_running:
            return await self.request_start(db, device, caller=caller, reason=reason)

        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[operator_restart_intent(device, node.port, settings=self._settings)],
            reason=reason,
            publisher=self._publisher,
        )
        if caller == "operator_restart":
            await self._review.clear_review_required(
                db, device, reason="Operator restarted Appium node", source="restart_node"
            )
        await db.refresh(node)
        return node
