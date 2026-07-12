"""High-level operator-driven Appium node lifecycle helpers.

All operator-initiated node lifecycle writes (start / stop / restart) must flow
through this module so that the ``device_intents`` table is the single source of
truth for desired ``appium_nodes`` state. Direct ``write_desired_state`` calls
from operator code are forbidden — they leave stale intent payloads that the
intent reconciler keeps re-asserting onto the AppiumNode row.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services import resource_service
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.core.timeutil import now_utc
from app.devices.models import DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    CommandKind,
    IntentRegistration,
)
from app.lifecycle.services import remediation_log
from app.packs.services.platform_resolver import applicable_resource_ports, resolve_pack_platform

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
    now = now_utc()
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


def operator_start_intent(device: Device, *, settings: SettingsReader) -> IntentRegistration:
    startup_timeout = settings.get_int("appium.startup_timeout_sec")
    viability_timeout = settings.get_int("general.session_viability_timeout_sec")
    # TTL replaces the node_running precondition (semantic delta #1): the row is a
    # no-op once the node runs (baseline:idle sustains running) and self-expires.
    expires_at = now_utc() + timedelta(seconds=startup_timeout + viability_timeout + 60)
    return IntentRegistration(
        source=operator_start_source(device.id),
        kind=CommandKind.operator_start,
        payload={"action": "start"},
        expires_at=expires_at,
    )


def operator_restart_intent(device: Device, *, settings: SettingsReader) -> IntentRegistration:
    window_sec = settings.get_int("appium_reconciler.restart_window_sec")
    requested_at = now_utc()
    deadline = requested_at + timedelta(seconds=window_sec)
    return IntentRegistration(
        source=operator_start_source(device.id),
        kind=CommandKind.operator_start,
        payload={
            "action": "start",
            "restart_requested_at": requested_at.isoformat(),
        },
        expires_at=deadline,
    )


def operator_stop_intents(device_id: uuid.UUID) -> list[IntentRegistration]:
    # No grid intent: the operator:stop:node hard stop already forces
    # accepting_new_sessions=False via the node_factor in intent_reconciler.
    return [
        IntentRegistration(
            source=f"operator:stop:node:{device_id}",
            kind=CommandKind.operator_stop,
            payload={"action": "stop"},
        ),
        # An operator stop is sticky: deny auto-recovery so the recovery availability
        # projection reports blocked (operator kind) and attempt_auto_recovery stands
        # down instead of spinning a doomed prio-20 start it can never make win against
        # this stop (N13). The operator-start path revokes this via operator_stop_sources.
        IntentRegistration(
            source=f"operator:stop:recovery:{device_id}",
            kind=CommandKind.operator_recovery_deny,
            payload={"allowed": False, "reason": "Operator stopped the node"},
        ),
    ]


async def _reserve_parallel_resources(db: AsyncSession, device: Device, *, node: AppiumNode) -> None:
    """Reserve host-unique parallel-resource ports (and derivedDataPath) for *node*.

    Claims persist across restarts (unique per node+capability, CASCADE on node
    delete). Recovery/policy/baseline restarts register start intents directly and
    never pass through here, so claims are deliberately NOT released on stop — the
    pull projection must keep serving them for those restarts.
    """
    if device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned")
    try:
        resolved = await resolve_pack_platform(
            db,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else None,
        )
    except LookupError:
        # No resolvable pack platform — the node still starts, just without
        # parallel-resource allocation (same fallback the push path had).
        return
    wanted = {p.capability_name: p.start for p in applicable_resource_ports(resolved, device.device_config)}
    claims = (await resource_service.get_port_claims_for_nodes(db, node_ids=[node.id])).get(node.id, {})
    if set(claims) - set(wanted):
        # device_config changed and a skip_when gate now excludes a claimed port. A
        # stale claim leaks into node capabilities and fails sessions that cannot
        # forward the port — drop this node's claims and re-reserve below.
        await resource_service.release_managed(db, node_id=node.id)
        claims = {}
    try:
        for capability_key, start_port in wanted.items():
            if capability_key not in claims:
                await resource_service.reserve(
                    db,
                    host_id=device.host_id,
                    capability_key=capability_key,
                    start_port=start_port,
                    node_id=node.id,
                )
    except resource_service.PoolExhaustedError as exc:
        raise NodeManagerError(str(exc)) from exc
    if resolved.parallel_resources.derived_data_path:
        allocated = await resource_service.get_capabilities(db, node_id=node.id)
        if "appium:derivedDataPath" not in allocated:
            await resource_service.set_node_extra_capability(
                db,
                node_id=node.id,
                capability_key="appium:derivedDataPath",
                value=f"/tmp/gridfleet/derived-data/{uuid.uuid4().hex}",
            )


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
            # the lowest free port for the host. Existing nodes keep their pinned
            # port — the reconciler applier pins the live node.port on start.
            port = (await candidate_ports(db, host_id=device.host_id, settings=self._settings))[0]
            node = AppiumNode(
                device_id=device.id,
                port=port,
            )
            db.add(node)
            await db.flush()
            device.appium_node = node

        await _reserve_parallel_resources(db, device, node=node)

        revoke_sources = list(operator_stop_sources(device.id))
        await IntentService(db).revoke_intents_and_reconcile(
            device_id=device.id,
            sources=revoke_sources,
            publisher=self._publisher,
        )
        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[operator_start_intent(device, settings=self._settings)],
            publisher=self._publisher,
        )
        if caller in {"operator_route", "operator_restart"}:
            ladder = await remediation_log.load_ladder(db, device.id)
            if ladder.episode_active:
                await remediation_log.append_reset(db, device.id, source="operator", action="operator_started")
            await self._review.clear_review_required(
                db, device, reason="Operator started Appium node", source="start_node"
            )
        await db.refresh(node)
        return node

    async def request_stop(self, db: AsyncSession, device: Device, *, reason: str) -> AppiumNode:
        """Register operator:stop intents (node + grid). Returns the node row for the
        convenience of route handlers.

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
            publisher=self._publisher,
        )
        await db.refresh(node)
        return node

    async def request_restart(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller, reason: str
    ) -> AppiumNode:
        """Register an operator:start intent in restart form (with fresh
        restart_requested_at + expires_at). If the node isn't currently observed running,
        fall back to request_start (no token, no deadline) — mirrors the existing
        bulk._bulk_restart_one fallback.
        """
        node: AppiumNode | None = device.appium_node
        if node is None or not node.observed_running:
            return await self.request_start(db, device, caller=caller, reason=reason)

        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[operator_restart_intent(device, settings=self._settings)],
            publisher=self._publisher,
        )
        if caller == "operator_restart":
            ladder = await remediation_log.load_ladder(db, device.id)
            if ladder.episode_active:
                await remediation_log.append_reset(db, device.id, source="operator", action="operator_restarted")
            await self._review.clear_review_required(
                db, device, reason="Operator restarted Appium node", source="restart_node"
            )
        await db.refresh(node)
        return node
