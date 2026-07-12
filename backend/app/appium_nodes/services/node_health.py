from __future__ import annotations

from datetime import timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.agent_comm.probe_result import ProbeResult, from_status_response
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import locking as appium_node_locking
from app.appium_nodes.services.common import node_state_severity
from app.core.metrics_recorders import record_background_loop_phase
from app.core.observability import get_logger
from app.core.timeutil import now_utc, parse_iso
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.event import record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    CommandKind,
    IntentRegistration,
)
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import escalate_device_remediation_failure
from app.lifecycle.services.incidents import LifecycleIncidentDetails

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.protocols import DeviceNodeHealthWriter, DeviceRecoveryControl
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.incidents import LifecycleIncidentService

logger = get_logger(__name__)
# Phase-metric label only: node health is now a host_sweep stage, not its own
# background loop, but its probe/apply timings still report under this name so
# existing dashboards survive the fold.
LOOP_NAME = "node_health"


class NodeHealthService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        recovery_control: DeviceRecoveryControl,
        health: DeviceNodeHealthWriter,
        incidents: LifecycleIncidentService,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._recovery_control = recovery_control
        self._health = health
        self._incidents = incidents

    async def fold_host_nodes(self, db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        """Fold pushed node_health facts into the durable node-health state.

        Entries match DB nodes by port; the entry's observed (pid,
        connection_target) must match the locked row or the observation predates
        a node change and is skipped. A DB-running node absent from the section
        is convergence's problem (appium_processes drives stop), not a health
        failure — it has no positive evidence, the push-era counterpart of the
        dial-era indeterminate-probe veto.
        """
        raw_nodes = section.get("nodes")
        if not isinstance(raw_nodes, list):
            return
        observed_at = parse_iso(section.get("reported_at")) or now_utc()
        by_port: dict[int, dict[str, Any]] = {
            entry["port"]: entry
            for entry in raw_nodes
            if isinstance(entry, dict) and isinstance(entry.get("port"), int)
        }
        stmt = (
            select(AppiumNode)
            .join(Device, Device.id == AppiumNode.device_id)
            .where(
                Device.host_id == host_id,
                AppiumNode.pid.is_not(None),
                AppiumNode.active_connection_target.is_not(None),
                # Don't judge nodes we are intentionally stopping (I1); the
                # locked re-check in _process_node_health covers the race where
                # desired_state flips to stopped after this SELECT.
                AppiumNode.desired_state == AppiumDesiredState.running,
            )
            .options(
                selectinload(AppiumNode.device).selectinload(Device.host),
                selectinload(AppiumNode.device).selectinload(Device.appium_node),
            )
            .order_by(AppiumNode.device_id)
        )
        nodes = (await db.execute(stmt)).scalars().all()
        apply_started = perf_counter()
        for node in nodes:
            entry = by_port.get(node.port)
            if entry is None:
                continue
            result = from_status_response(entry)
            try:
                locked_device = await device_locking.lock_device(db, node.device_id, load_sessions=True)
            except NoResultFound:
                logger.warning("Node health fold skipped: device %s no longer exists", node.device_id)
                await db.commit()
                continue

            await self._process_node_health(
                db,
                node,
                locked_device,
                result=result,
                observed_port=node.port,
                observed_pid=entry.get("pid") if isinstance(entry.get("pid"), int) else None,
                observed_active_connection_target=(
                    entry.get("connection_target") if isinstance(entry.get("connection_target"), str) else None
                ),
                observed_at=observed_at,
            )
            await db.commit()
        record_background_loop_phase(LOOP_NAME, "apply", perf_counter() - apply_started)

    async def _attempt_node_restart(self, db: AsyncSession, *, device: Device) -> None:
        node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one_or_none()
        if node is None:
            return
        window_sec = self._settings.get_int("appium_reconciler.restart_window_sec")
        requested_at = now_utc()
        deadline = requested_at + timedelta(seconds=window_sec)
        # The command row TTL bounds the restart intent row; the watermark bounds
        # the process spawn time the agent must satisfy.
        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=f"auto_recovery:node:{device.id}",
                    kind=CommandKind.auto_recovery_start,
                    payload={
                        "action": "start",
                        "restart_requested_at": requested_at.isoformat(),
                    },
                    expires_at=deadline,
                ),
                IntentRegistration(
                    source=f"auto_recovery:recovery:{device.id}",
                    kind=CommandKind.auto_recovery_allow,
                    payload={"allowed": True, "reason": "Node health restart"},
                    expires_at=deadline,
                ),
            ],
            publisher=self._publisher,
        )
        await db.commit()

    async def _process_node_health(
        self,
        db: AsyncSession,
        node: AppiumNode,
        device: Device,
        *,
        result: ProbeResult,
        observed_port: int | None = None,
        observed_pid: int | None = None,
        observed_active_connection_target: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
        if locked_node is None:
            # Node was deleted between the caller's lock_device and here. Bail out
            # quietly; lower layers will reconcile on the next sweep.
            return

        if observed_port is not None and (
            locked_node.port != observed_port
            or locked_node.pid != observed_pid
            or locked_node.active_connection_target != observed_active_connection_target
        ):
            logger.info(
                "Node health check for device %s skipped stale probe result after node changed",
                device.name,
            )
            return

        node = locked_node

        if locked_node.pid is None or locked_node.active_connection_target is None:
            return

        if locked_node.desired_state == AppiumDesiredState.stopped:
            # Intentional-stop veto (I1): the node is being torn down on purpose
            # (desired_state=stopped) but is still observed_running until the stop
            # propagates. A refused/failed probe in that window is EXPECTED
            # teardown, not a health failure — counting it would escalate an
            # auto-recovery restart that fights the stop. The reconciler owns
            # driving it to stopped. Re-checked here under the row lock because
            # desired_state can flip to stopped during the async probe gather,
            # after the fold's unlocked SELECT filtered on it.
            return

        if result.status == "indeterminate":
            # Indeterminate-probe veto: a network error against the agent is
            # inconclusive evidence. Never count it as a failure and never
            # drive recovery from it — only positive probe evidence
            # (``ack``/``refused``) moves the node's health state.
            return

        healthy = result.status == "ack"

        if healthy:
            if locked_node.health_failing_since is not None:
                logger.info("Node for device %s (%s) recovered", device.name, device.identity_value)
                await self._recovery_control.record_control_action(
                    db,
                    device,
                    action="node_monitor_recovered",
                    failure_source="node_health",
                    failure_reason="Node health checks recovered",
                )
                # The dedicated ``lifecycle_recovered`` incident below already
                # describes the recovery; pass ``record_incident=False`` to avoid
                # publishing a duplicate ``lifecycle_recovered`` event for the
                # same recovery moment.
                await self._recovery_control.clear_pending_auto_stop_on_recovery(
                    db,
                    device,
                    source="node_health",
                    reason="Node health checks recovered",
                    record_incident=False,
                )
                self._publisher.queue_for_session(
                    db,
                    "node.state_changed",
                    {
                        "device_id": str(device.id),
                        "device_name": device.name,
                        "old_state": "error",
                        "new_state": "running",
                        "port": node.port,
                    },
                    severity=node_state_severity("error", "running"),
                )
                await record_event(
                    db,
                    device.id,
                    DeviceEventType.node_restart,
                    {"recovered_from": "health_check_failure", "port": node.port},
                )
                await self._incidents.record_lifecycle_incident(
                    db,
                    device,
                    DeviceEventType.lifecycle_recovered,
                    LifecycleIncidentDetails(
                        summary_state=DeviceLifecyclePolicySummaryState.idle,
                        reason="Node health checks recovered",
                        detail="The node resumed healthy operation after transient failures",
                        source="node_health",
                    ),
                )
            locked_node.health_failing_since = None
            # Direct probe acked: the node is the authoritative health signal.
            # Persist the positive result truthfully — health_running=True —
            # instead of clearing the columns to NULL and relying on the
            # pid-based fallback in node_running_signal. ``health_state`` is
            # cleared so the public summary label stays "running" rather than
            # echoing an "error" stamp.
            await self._health.apply_node_state_transition(
                db,
                device,
                health_running=True,
                health_state=None,
                mark_offline=False,
            )
            return

        await self._record_health_failure(db, node, locked_node, device, observed_at=observed_at or now_utc())

    async def _record_health_failure(
        self,
        db: AsyncSession,
        node: AppiumNode,
        locked_node: AppiumNode,
        device: Device,
        *,
        observed_at: datetime,
    ) -> None:
        if locked_node.health_failing_since is None:
            locked_node.health_failing_since = observed_at
            onset = True
        else:
            onset = False
        failing_for_sec = max(0.0, (observed_at - locked_node.health_failing_since).total_seconds())
        window_sec = float(self._settings.get("general.node_fail_window_sec"))
        verdict = (onset and window_sec <= 0) or (
            not onset and observed_at > locked_node.health_failing_since and failing_for_sec >= window_sec
        )
        await self._health.apply_node_state_transition(
            db,
            device,
            health_running=False,
            health_state="error",
            mark_offline=verdict,
        )
        logger.warning(
            "Node health check failed for device %s (port %d): %.0fs/%.0fs",
            device.name,
            node.port,
            failing_for_sec,
            window_sec,
        )
        if onset:
            await record_event(db, device.id, DeviceEventType.health_check_fail, {"port": node.port})

        if verdict:
            await record_event(
                db,
                device.id,
                DeviceEventType.health_check_fail,
                {"failing_for_sec": int(failing_for_sec), "port": node.port},
            )
            locked_node.health_failing_since = observed_at

            ladder = await remediation_log.load_ladder(db, device.id)
            deadline = ladder.backoff_active(now=now_utc())
            if deadline is not None:
                logger.warning(
                    "Node for device %s reached failure window; restart deferred by shared backoff until %s",
                    device.name,
                    deadline.isoformat(),
                )
                return

            outcome = await escalate_device_remediation_failure(
                db,
                device,
                settings=self._settings,
                source="node_health",
                reason="Node health checks kept failing; automated restart escalation",
            )
            if outcome.shelved:
                logger.error(
                    "Node for device %s exhausted automated restarts (%d attempts); shelved for operator review",
                    device.name,
                    outcome.attempts,
                )
                return

            logger.error("Node for device %s reached failure window, attempting restart", device.name)
            await self._attempt_node_restart(db, device=device)
