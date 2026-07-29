"""Repository-wide transaction-boundary contract (Phase 10, final gate).

Everything the phase-scoped guards asserted file by file is asserted here for
every module under ``backend/app``, with no path prefixes, wildcards, line
numbers or "all functions in this file" entries anywhere:

* no direct ``.commit()`` / ``.rollback()`` anywhere, with no allowlist;
* no ``commit`` / ``rollback`` / ``autocommit`` parameter, anywhere, at all;
* every ``begin()`` context has a named, individually classified owner in
  ``BEGIN_OWNER_REGISTRY``;
* every ``begin_nested()`` has a named owner in ``BEGIN_NESTED_ALLOWLIST``;
* every agent/Appium/HTTP/sleep/subprocess/filesystem effect has a named owner in
  ``EFFECT_ENTRY_POINTS``, and none of them runs inside a transaction block.

Each registry is compared by set *equality*, never by subset, so it can only
shrink: converting a function without deleting its entry fails just as loudly as
a new violation appearing somewhere else. Populate a registry by emptying it,
running the test and pasting the printed inventory — never by guessing names.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
PRODUCTION = sorted(APP_ROOT.rglob("*.py"))

TRANSACTION_CONTROL_ARGUMENTS = frozenset({"commit", "rollback", "autocommit"})

# Three owners, each load-bearing for a *named* partial-failure behaviour. The
# set can only shrink; a fourth savepoint anywhere fails this contract.
BEGIN_NESTED_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # An UnknownMemberOfError raised while inserting member_of edges must not
        # abort the caller's transaction, so the insert runs inside its own
        # savepoint that can be rolled back alone. Permanent, not deferred.
        ("app/devices/services/groups.py", "_replace_member_of"),
        # A cooldown clear that fails must not abort the whole reconcile pass;
        # the savepoint keeps the other candidates' work.
        ("app/devices/services/intent_reconciler.py", "_apply_candidate_hygiene"),
        # Phase 7's row seam: one bad import row rolls back alone, the rest of
        # the bundle still lands.
        ("app/portability/services/import_bundle.py", "PortabilityImportService._insert_row_with_savepoint"),
    }
)


@dataclass(frozen=True, slots=True)
class BoundaryOwner:
    """One named owner of a ``begin()`` context.

    ``kind`` is deliberately binary. **command** means the transaction is one
    domain use case — an operator/agent request, a router handler, a per-item
    domain action, a discrete service command — and maps to observable public
    behaviour. **infrastructure** means the transaction is process plumbing:
    scheduler loops and their stages, the retry wrapper, the metrics flush, the
    event outbox, the durable-job queue. A helper that is honestly neither must
    lose its ``begin()`` and receive the caller's active session instead of
    growing a third kind here.
    """

    module: str
    qualified_function: str
    kind: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.module, self.qualified_function)


BEGIN_OWNER_REGISTRY: frozenset[BoundaryOwner] = frozenset(
    {
        # --- commands: routers and operator-facing handlers ---
        BoundaryOwner("app/appium_nodes/routers/nodes.py", "_apply_startable_lever", "command"),
        BoundaryOwner("app/appium_nodes/routers/nodes.py", "stop_node", "command"),
        BoundaryOwner("app/devices/routers/control.py", "_clear_session_viability_after_reconnect", "command"),
        BoundaryOwner("app/devices/routers/control.py", "enter_device_maintenance", "command"),
        BoundaryOwner("app/devices/routers/control.py", "exit_device_maintenance", "command"),
        BoundaryOwner("app/devices/routers/control.py", "merge_device_config", "command"),
        BoundaryOwner("app/devices/routers/control.py", "reconnect_device", "command"),
        BoundaryOwner("app/devices/routers/core.py", "delete_device", "command"),
        BoundaryOwner("app/devices/routers/core.py", "update_device", "command"),
        BoundaryOwner("app/devices/routers/groups.py", "create_group", "command"),
        BoundaryOwner("app/devices/routers/groups.py", "add_members", "command"),
        BoundaryOwner("app/devices/routers/groups.py", "delete_group", "command"),
        BoundaryOwner("app/devices/routers/groups.py", "remove_members", "command"),
        BoundaryOwner("app/devices/routers/groups.py", "update_group", "command"),
        BoundaryOwner("app/devices/routers/test_data.py", "merge_test_data", "command"),
        BoundaryOwner("app/devices/routers/test_data.py", "replace_test_data", "command"),
        BoundaryOwner("app/hosts/router.py", "_register_host_txn", "command"),
        BoundaryOwner("app/hosts/router.py", "approve_host", "command"),
        BoundaryOwner("app/hosts/router.py", "confirm_discovery", "command"),
        BoundaryOwner("app/hosts/router.py", "create_host", "command"),
        BoundaryOwner("app/hosts/router.py", "delete_host", "command"),
        BoundaryOwner("app/hosts/router.py", "put_host_tool_env", "command"),
        BoundaryOwner("app/hosts/router.py", "register_host", "command"),
        BoundaryOwner("app/hosts/router.py", "reject_host", "command"),
        BoundaryOwner("app/hosts/router.py", "trigger_driver_doctor", "command"),
        BoundaryOwner("app/hosts/router_agent.py", "status", "command"),
        BoundaryOwner("app/packs/routers/catalog.py", "delete_driver_pack", "command"),
        BoundaryOwner("app/packs/routers/catalog.py", "update_pack", "command"),
        BoundaryOwner("app/packs/routers/catalog.py", "update_runtime_policy", "command"),
        BoundaryOwner("app/packs/routers/uploads.py", "delete_release", "command"),
        BoundaryOwner("app/packs/routers/uploads.py", "update_current_release", "command"),
        BoundaryOwner("app/packs/routers/uploads.py", "upload", "command"),
        BoundaryOwner("app/sessions/router.py", "update_session_status", "command"),
        BoundaryOwner("app/grid/router_internal.py", "_finalize_interrupted_create", "command"),
        BoundaryOwner("app/grid/router_internal.py", "activity", "command"),
        BoundaryOwner("app/grid/router_internal.py", "cancel_ticket", "command"),
        BoundaryOwner("app/grid/router_internal.py", "create_session", "command"),
        # --- commands: observation folds. One fold call is one host's ingest use
        # case; the inventory read and each device/node land in their own
        # transaction inside it.
        BoundaryOwner("app/appium_nodes/services/node_health.py", "NodeHealthService.fold_host_nodes", "command"),
        BoundaryOwner("app/appium_nodes/services/reconciler.py", "apply_observed_node_command", "command"),
        BoundaryOwner("app/devices/services/connectivity.py", "ConnectivityService.fold_host_devices", "command"),
        BoundaryOwner(
            "app/devices/services/property_refresh.py",
            "PropertyRefreshService.fold_host_device_properties",
            "command",
        ),
        BoundaryOwner(
            "app/hosts/service_resource_telemetry.py",
            "HostResourceTelemetryService.fold_host_telemetry",
            "command",
        ),
        BoundaryOwner("app/hosts/service_status_push.py", "HostStatusPushService.process_prepublication", "command"),
        # --- commands: per-item domain actions driven by a bulk or loop caller ---
        BoundaryOwner("app/devices/services/bulk.py", "BulkOperationsService.bulk_delete._one", "command"),
        BoundaryOwner("app/devices/services/bulk.py", "BulkOperationsService.bulk_exit_maintenance._one", "command"),
        BoundaryOwner("app/devices/services/bulk.py", "_run_per_device_action._one", "command"),
        BoundaryOwner("app/devices/services/intent_reconciler.py", "reconcile_device_command", "command"),
        BoundaryOwner("app/devices/services/maintenance.py", "MaintenanceService.schedule_device_recovery", "command"),
        BoundaryOwner("app/devices/services/remediation_job.py", "RemediationJobService._fail_claim", "command"),
        BoundaryOwner("app/devices/services/remediation_job.py", "RemediationJobService._finalize", "command"),
        BoundaryOwner("app/devices/services/remediation_job.py", "RemediationJobService._prepare", "command"),
        BoundaryOwner(
            "app/lifecycle/services/recovery_job.py",
            "RecoveryJobService._clear_generation_and_fail",
            "command",
        ),
        BoundaryOwner("app/lifecycle/services/recovery_job.py", "RecoveryJobService._ensure_prepared", "command"),
        BoundaryOwner("app/lifecycle/services/recovery_job.py", "RecoveryJobService._finalize_device", "command"),
        BoundaryOwner("app/lifecycle/services/recovery_job.py", "RecoveryJobService._finalize_job", "command"),
        BoundaryOwner("app/grid/session_create.py", "_fail", "command"),
        BoundaryOwner("app/grid/session_create.py", "create_and_promote", "command"),
        BoundaryOwner("app/grid/session_create.py", "mark_target_node_down", "command"),
        BoundaryOwner("app/runs/service_allocator.py", "RunAllocatorService.create_run", "command"),
        BoundaryOwner("app/runs/service_lifecycle.py", "RunLifecycleService._run_deferred_stops", "command"),
        BoundaryOwner("app/runs/service_lifecycle_failures.py", "RunFailureService.cooldown_device", "command"),
        BoundaryOwner(
            "app/runs/service_lifecycle_failures.py",
            "RunFailureService.report_preparation_failure",
            "command",
        ),
        BoundaryOwner("app/runs/service_teardown.py", "RunTeardownService._fail_job", "command"),
        BoundaryOwner("app/runs/service_teardown.py", "RunTeardownService._run_deferred_stops", "command"),
        BoundaryOwner("app/sessions/service_kill.py", "SessionKillService._fail_job", "command"),
        BoundaryOwner("app/sessions/service_kill.py", "SessionKillService.finalize", "command"),
        BoundaryOwner("app/sessions/service_kill.py", "SessionKillService.prepare", "command"),
        BoundaryOwner("app/sessions/service_sync.py", "SessionSyncService._close_session_locked", "command"),
        BoundaryOwner(
            "app/sessions/service_sync.py",
            "SessionSyncService._restore_device_after_session_end",
            "command",
        ),
        BoundaryOwner("app/sessions/service_viability.py", "SessionViabilityService._confirm_probe", "command"),
        BoundaryOwner(
            "app/sessions/service_viability.py",
            "SessionViabilityService._escalate_probe_failure_command",
            "command",
        ),
        BoundaryOwner("app/sessions/service_viability.py", "SessionViabilityService._finalize_probe", "command"),
        BoundaryOwner("app/sessions/service_viability.py", "SessionViabilityService._prepare_probe", "command"),
        BoundaryOwner("app/settings/service.py", "SettingsService._run_mutation", "command"),
        BoundaryOwner(
            "app/portability/services/import_bundle.py", "PortabilityImportService._commit_group_definitions", "command"
        ),
        BoundaryOwner(
            "app/portability/services/import_bundle.py", "PortabilityImportService._insert_device_batches", "command"
        ),
        BoundaryOwner(
            "app/portability/services/import_bundle.py", "PortabilityImportService._stage_static_memberships", "command"
        ),
        BoundaryOwner(
            "app/verification/services/execution.py", "VerificationExecutionService._finalize_failure", "command"
        ),
        BoundaryOwner(
            "app/verification/services/execution.py", "VerificationExecutionService._finalize_success", "command"
        ),
        BoundaryOwner(
            "app/verification/services/execution.py", "VerificationExecutionService._prepare_node", "command"
        ),
        BoundaryOwner(
            "app/verification/services/execution.py", "VerificationExecutionService._run_probe_phase", "command"
        ),
        BoundaryOwner(
            "app/verification/services/execution.py",
            "VerificationExecutionService._run_probe_phase._promote",
            "command",
        ),
        BoundaryOwner(
            "app/verification/services/execution.py",
            "VerificationExecutionService._stop_existing_node",
            "command",
        ),
        BoundaryOwner(
            "app/verification/services/preparation.py",
            "VerificationPreparationService.prepare_create",
            "command",
        ),
        BoundaryOwner(
            "app/verification/services/preparation.py",
            "VerificationPreparationService.prepare_update",
            "command",
        ),
        BoundaryOwner(
            "app/verification/services/service.py",
            "VerificationService.start_existing_device_verification_job",
            "command",
        ),
        BoundaryOwner("app/verification/services/service.py", "VerificationService.start_verification_job", "command"),
        # --- infrastructure: scheduler loops, their stages, and process plumbing ---
        BoundaryOwner("app/appium_nodes/services/host_sweep.py", "run_host_sweep_once._sweep_host", "infrastructure"),
        BoundaryOwner("app/appium_nodes/services/reconciler.py", "_record_start_failure", "infrastructure"),
        BoundaryOwner("app/appium_nodes/services/reconciler.py", "_reset_start_failure", "infrastructure"),
        BoundaryOwner("app/appium_nodes/services/reconciler.py", "_touch_last_observed", "infrastructure"),
        BoundaryOwner(
            "app/appium_nodes/services/status_fold_loop.py", "StatusFoldLoop._advance_applied", "infrastructure"
        ),
        BoundaryOwner("app/core/db_retry.py", "retry_on_serialization_failure", "infrastructure"),
        BoundaryOwner("app/core/observability.py", "flush_background_loop_snapshots", "infrastructure"),
        BoundaryOwner("app/devices/services/data_cleanup.py", "_delete_in_batches", "infrastructure"),
        BoundaryOwner(
            "app/devices/services/fleet_capacity.py",
            "FleetCapacityService.collect_capacity_snapshot_once",
            "infrastructure",
        ),
        BoundaryOwner(
            "app/devices/services/intent_reconciler.py",
            "run_device_intent_reconciler_once",
            "infrastructure",
        ),
        BoundaryOwner("app/events/event_bus.py", "EventBus.publish", "infrastructure"),
        BoundaryOwner("app/grid/allocation_reaper.py", "GridAllocationReaperLoop.run_cycle", "infrastructure"),
        BoundaryOwner("app/jobs/queue.py", "DurableJobService.reset_stale_running_jobs", "infrastructure"),
        BoundaryOwner("app/jobs/queue.py", "DurableJobService.run_pending_once", "infrastructure"),
        BoundaryOwner("app/main.py", "_build_janitor._pack_drain_stage", "infrastructure"),
        BoundaryOwner("app/packs/services/release_rollout.py", "run_release_rollout_stage", "infrastructure"),
        BoundaryOwner("app/runs/service_reaper.py", "reap_stale_runs", "infrastructure"),
        BoundaryOwner("app/verification/services/job_state.py", "publish", "infrastructure"),
    }
)

# Agent/Appium/HTTP entry points, waits, subprocess spawns and filesystem writes.
# Matched on the call's last segment so an import-style change cannot empty the
# scan; ``APPIUM_DIRECT_NAMES`` additionally requires the module to import
# ``appium_direct``, because those five names collide with ordinary DB reads
# (``session_services.crud.list_sessions``).
AGENT_EFFECT_NAMES = frozenset(
    {
        "agent_health",
        "agent_nodes_refresh",
        "appium_logs",
        "appium_status",
        "converge_device_now",
        "dispatch_recommended_action",
        "fetch_pack_candidates",
        "fetch_pack_device_health",
        "get_agent_tool_status",
        "get_pack_devices",
        "get_tool_status",
        "normalize_pack_device",
        "pack_device_health",
        "pack_device_lifecycle_action",
        "pack_doctor",
        "poke_node_refresh",
        "poke_node_refresh_target",
        "_agent_get_pack_devices",
    }
)
APPIUM_DIRECT_NAMES = frozenset(
    {"create_session", "create_session_raw", "list_sessions", "session_alive", "terminate_session"}
)
WAIT_NAMES = frozenset({"sleep"})
SUBPROCESS_NAMES = frozenset(
    {
        "Popen",
        "check_call",
        "check_output",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "os.system",
        "subprocess.run",
    }
)
FILESYSTEM_NAMES = frozenset(
    {
        "extractall",
        "mkdir",
        "os.remove",
        "os.rename",
        "os.replace",
        "rmtree",
        "shutil.copy",
        "shutil.move",
        "shutil.rmtree",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)
BARE_EFFECT_NAMES = AGENT_EFFECT_NAMES | WAIT_NAMES | SUBPROCESS_NAMES | FILESYSTEM_NAMES

# The complete production inventory of the above. Set equality: a new agent dial,
# retry sleep, subprocess spawn or file write anywhere under ``app/`` has to be
# named here, which is what forces it through review.
EFFECT_ENTRY_POINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("app/agent_comm/node_poke.py", "poke_node_refresh"),
        ("app/agent_comm/node_poke.py", "poke_node_refresh_target"),
        ("app/appium_nodes/routers/nodes.py", "_poke_agent"),
        ("app/appium_nodes/services/heartbeat.py", "_ping_agent"),
        ("app/appium_nodes/services/reconciler.py", "ReconcilerService.converge_device_now"),
        ("app/core/background_loop.py", "BackgroundLoop._wait"),
        ("app/core/db_retry.py", "retry_on_serialization_failure"),
        ("app/devices/routers/control.py", "device_health"),
        ("app/devices/routers/control.py", "device_lifecycle_action"),
        ("app/devices/routers/control.py", "device_logs"),
        ("app/devices/routers/control.py", "reconnect_device"),
        ("app/devices/services/bulk.py", "BulkOperationsService.bulk_reconnect._reconnect_one"),
        ("app/devices/services/intent_reconciler.py", "_reconcile_and_deliver"),
        ("app/devices/services/link_repair.py", "dispatch_recommended_action"),
        ("app/devices/services/remediation_job.py", "RemediationJobService._dispatch"),
        ("app/events/event_bus.py", "EventBus._listen_for_notifications"),
        ("app/events/event_bus.py", "EventBus._poll_for_missed_events"),
        ("app/grid/router_internal.py", "_finalize_interrupted_create"),
        ("app/grid/router_internal.py", "create_session"),
        ("app/grid/session_create.py", "_sweep_target"),
        ("app/grid/session_create.py", "create_and_promote"),
        ("app/hosts/router.py", "_auto_discover"),
        ("app/hosts/router.py", "_fetch_candidates"),
        ("app/hosts/router.py", "get_host_tool_status"),
        ("app/hosts/router.py", "trigger_driver_doctor"),
        ("app/jobs/queue.py", "DurableJobWorkerLoop._wait"),
        ("app/lifecycle/services/recovery_job.py", "RecoveryJobService._run_probe"),
        ("app/lifecycle/services/recovery_job.py", "RecoveryJobService._wait_for_node_running"),
        ("app/main.py", "_scheduler_stall_watchdog"),
        ("app/packs/services/discovery.py", "PackDiscoveryService.fetch_pack_candidates"),
        ("app/packs/services/service.py", "unlink_pack_artifact"),
        ("app/packs/services/storage.py", "PackStorageService._ensure_root"),
        ("app/packs/services/storage.py", "PackStorageService.store"),
        ("app/runs/service_allocator.py", "RunAllocatorService.create_run"),
        ("app/runs/service_lifecycle_failures.py", "RunFailureService.cooldown_device"),
        ("app/runs/service_lifecycle_failures.py", "RunFailureService.report_preparation_failure"),
        ("app/runs/service_teardown.py", "perform_run_teardown_effect"),
        ("app/runs/service_teardown.py", "perform_run_teardown_effect.terminate"),
        ("app/scripts/dump_openapi.py", "main"),
        ("app/sessions/service_kill.py", "_perform_kill_effect"),
        ("app/sessions/service_sync.py", "SessionSyncService._enumerate_orphan_targets._enumerate"),
        ("app/sessions/service_sync.py", "SessionSyncService._probe_liveness_targets._probe"),
        ("app/sessions/service_sync.py", "SessionSyncService._terminate_orphans"),
        ("app/sessions/service_sync.py", "_terminate_for_close"),
        ("app/sessions/service_viability.py", "SessionViabilityService.probe_session_direct"),
        ("app/sessions/service_viability.py", "_terminate_probe_session"),
        ("app/verification/services/execution.py", "VerificationExecutionService._prepare_node"),
        ("app/verification/services/execution.py", "VerificationExecutionService.run_device_health"),
        ("app/verification/services/execution.py", "VerificationExecutionService.wait_for_node_running"),
        ("app/verification/services/preparation.py", "VerificationPreparationService._apply_host_resolution"),
        ("app/verification/services/preparation.py", "VerificationPreparationService._apply_pack_normalization"),
    }
)


@dataclass(frozen=True, slots=True)
class RemoteEffectOwner:
    """An effect owner whose no-active-transaction property has a runtime test.

    The static ``test_no_effect_runs_inside_a_transaction_block`` below covers
    every entry point in ``EFFECT_ENTRY_POINTS`` lexically. These entries are the
    subset that Phases 4, 8, 9 and 10 additionally pinned at *runtime* — a real
    session watching a real dial — because lexical nesting cannot see an effect
    reached through a callee.
    """

    module: str
    qualified_function: str
    test_nodeid: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.module, self.qualified_function)


REMOTE_EFFECT_OWNER_REGISTRY: frozenset[RemoteEffectOwner] = frozenset(
    {
        # Phase 4: the direct-to-Appium session effects.
        RemoteEffectOwner(
            "app/grid/router_internal.py",
            "_finalize_interrupted_create",
            "tests/grid/test_grid_router_internal_units.py"
            "::test_resume_interrupted_terminates_appium_with_no_open_transaction",
        ),
        RemoteEffectOwner(
            "app/sessions/service_kill.py",
            "_perform_kill_effect",
            "tests/sessions/test_sessions_kill_api.py::test_kill_delete_sees_no_active_transaction",
        ),
        # Phase 8/9: the host and pack agent dials.
        RemoteEffectOwner(
            "app/hosts/router.py",
            "trigger_driver_doctor",
            "tests/hosts/test_phase9_host_remote_boundaries.py"
            "::test_pack_doctor_dials_the_agent_with_no_open_transaction",
        ),
        RemoteEffectOwner(
            "app/hosts/router.py",
            "get_host_tool_status",
            "tests/hosts/test_phase9_host_remote_boundaries.py"
            "::test_tool_status_dials_the_agent_with_no_open_transaction",
        ),
        RemoteEffectOwner(
            "app/hosts/router.py",
            "_fetch_candidates",
            "tests/hosts/test_phase9_host_remote_boundaries.py"
            "::test_discovery_routes_dial_the_agent_with_no_open_transaction",
        ),
        RemoteEffectOwner(
            "app/hosts/router.py",
            "_auto_discover",
            "tests/hosts/test_phase9_host_remote_boundaries.py"
            "::test_auto_discover_dials_the_agent_with_no_open_transaction",
        ),
        # Phase 10: the remediation dispatch and the recovery runner's waits.
        RemoteEffectOwner(
            "app/devices/services/remediation_job.py",
            "RemediationJobService._dispatch",
            "tests/jobs/test_remediation_transaction_boundaries.py"
            "::test_agent_action_runs_with_no_open_transaction_on_copied_scalars",
        ),
        RemoteEffectOwner(
            "app/lifecycle/services/recovery_job.py",
            "RecoveryJobService._run_probe",
            "tests/jobs/test_device_recovery.py::test_recovery_job_runs_probe_with_no_open_transaction",
        ),
        RemoteEffectOwner(
            "app/lifecycle/services/recovery_job.py",
            "RecoveryJobService._wait_for_node_running",
            "tests/jobs/test_device_recovery_more.py::test_polling_sleeps_observe_no_open_transaction",
        ),
    }
)

# The set can grow but never shrink. Nine entries, contributed by:
#   Phase 4 (2): grid/router_internal._finalize_interrupted_create, sessions/service_kill._perform_kill_effect
#   Phase 8/9 (4): the four hosts/router agent dials
#   Phase 10 (3): remediation_job._dispatch, recovery_job._run_probe, recovery_job._wait_for_node_running
# Equality with EFFECT_ENTRY_POINTS is impossible (51 lexical entry points vs 9
# with runtime tests), so this floor is what stops the set silently emptying.
REMOTE_EFFECT_OWNER_FLOOR = 9


def relative_module(path: Path) -> str:
    return str(path.relative_to(BACKEND_ROOT))


def iter_owned(node: ast.AST, owner: str) -> Iterator[tuple[ast.AST, str]]:
    """Yield every descendant of *node* paired with its innermost enclosing function.

    Scope names nest, so a method reports as ``Class.method`` and a closure as
    ``outer.inner``. A call in a decorator or default argument is attributed to
    the function it decorates, which errs toward naming something rather than
    ``<module>``.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            nested = f"{owner}.{child.name}" if owner else child.name
            yield child, nested
            yield from iter_owned(child, nested)
        else:
            yield child, owner
            yield from iter_owned(child, owner)


def parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _owner_name(owner: str) -> str:
    return owner or "<module>"


def call_tail(node: ast.expr) -> str | None:
    """The last segment of a call target: ``a.b.c()`` -> ``"c"``, ``c()`` -> ``"c"``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _dotted_name(node: ast.expr) -> str | None:
    """The full dotted target when it is a plain attribute chain, else ``None``."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _attribute_call_owners(attribute: str) -> list[tuple[str, str, int]]:
    findings: list[tuple[str, str, int]] = []
    for path in PRODUCTION:
        module = relative_module(path)
        for node, owner in iter_owned(parse_module(path), ""):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attribute:
                findings.append((module, _owner_name(owner), node.lineno))
    return findings


def transaction_control_owners() -> list[tuple[str, str, int]]:
    """``(module, qualified_function, lineno)`` for every ``.commit()``/``.rollback()``."""
    return sorted(_attribute_call_owners("commit") + _attribute_call_owners("rollback"))


def begin_nested_owners() -> list[tuple[str, str, int]]:
    return sorted(_attribute_call_owners("begin_nested"))


def _opens_transaction(item: ast.withitem) -> bool:
    context = item.context_expr
    return (
        isinstance(context, ast.Call)
        and isinstance(context.func, ast.Attribute)
        and context.func.attr in {"begin", "begin_nested"}
    )


def begin_owner_findings(tree: ast.Module, module: str) -> list[tuple[str, str, int]]:
    """Every ``begin()`` context in *tree*, by owner.

    Two shapes, because both open a transaction that outlives the statement:
    a ``with``/``async with`` item, and a ``begin()`` handed to an exit stack's
    ``enter_context``/``enter_async_context``. ``begin_nested()`` has its own
    attribute name and is checked separately.

    WHAT THIS DOES NOT SEE, decided rather than patched. The stack shape is
    matched by *name*, positionally: ``enter_async_context(cm=db.begin())``
    (the loop reads ``node.args``, never ``node.keywords``), an aliased argument
    (``cm = db.begin()`` then ``enter_async_context(cm)``), ``push`` and
    ``push_async_exit``, and any indirection through another call frame all
    escape it. The discriminator is name-only in the other direction too:
    ``stack.enter_context(unrelated.begin())`` would be flagged as a
    transaction. Each of those is one more shape, not the end of the class --
    closing the class needs transaction-context dataflow beyond the effect
    walker's stack-receiver binding state. None of these shapes appears anywhere
    under ``app/`` today; ``grep -rn
    "enter_async_context\\|AsyncExitStack" app/`` returning nothing is what makes
    the disclosure sufficient, and a first exit-stack boundary landing in
    production is the trigger to revisit it.
    """
    findings: list[tuple[str, str, int]] = []
    for node, owner in iter_owned(tree, ""):
        if isinstance(node, ast.With | ast.AsyncWith):
            findings.extend(
                (module, _owner_name(owner), item.context_expr.lineno)
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "begin"
            )
        elif isinstance(node, ast.Call) and call_tail(node.func) in {"enter_context", "enter_async_context"}:
            findings.extend(
                (module, _owner_name(owner), argument.lineno)
                for argument in node.args
                if isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Attribute)
                and argument.func.attr == "begin"
            )
    return findings


def begin_owners() -> list[tuple[str, str, int]]:
    """``(module, qualified_function, lineno)`` for every ``begin()`` context, whether
    opened by ``async with`` or handed to an exit stack. See ``begin_owner_findings``.
    """
    findings: list[tuple[str, str, int]] = []
    for path in PRODUCTION:
        findings.extend(begin_owner_findings(parse_module(path), relative_module(path)))
    return sorted(findings)


def transaction_control_arguments() -> list[tuple[str, str, str, int]]:
    findings: list[tuple[str, str, str, int]] = []
    for path in PRODUCTION:
        module = relative_module(path)
        for node, owner in iter_owned(parse_module(path), ""):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            declared = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            findings.extend(
                (module, _owner_name(owner), argument.arg, node.lineno)
                for argument in declared
                if argument.arg in TRANSACTION_CONTROL_ARGUMENTS
            )
    return sorted(findings)


def _effect_call_name(node: ast.Call, *, module_uses_appium_direct: bool) -> str | None:
    tail = call_tail(node.func)
    if tail is None:
        return None
    dotted = _dotted_name(node.func)
    if dotted in SUBPROCESS_NAMES or dotted in FILESYSTEM_NAMES:
        return dotted
    if tail in BARE_EFFECT_NAMES:
        return tail
    if tail in APPIUM_DIRECT_NAMES and module_uses_appium_direct:
        return tail
    return None


def _uses_appium_direct(tree: ast.Module) -> bool:
    return any(
        "appium_direct" in {alias.name for alias in node.names} or "appium_direct" in (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )


def effect_owners() -> list[tuple[str, str, int]]:
    """``(module, qualified_function, lineno)`` for every remote/wait/spawn/write effect."""
    findings: list[tuple[str, str, int]] = []
    for path in PRODUCTION:
        module = relative_module(path)
        tree = parse_module(path)
        appium = _uses_appium_direct(tree)
        for node, owner in iter_owned(tree, ""):
            if isinstance(node, ast.Call) and _effect_call_name(node, module_uses_appium_direct=appium) is not None:
                findings.append((module, _owner_name(owner), node.lineno))
    return sorted(findings)


def _registers_transaction_on_a_stack(node: ast.AST) -> frozenset[str]:
    """Exit-stack receiver names handed a ``begin()`` in *node*'s executable subtree.

    The mirror of the second shape ``begin_owner_findings`` detects. A
    transaction registered on a stack stays open until the stack unwinds, so
    every statement after the registration in that block runs with it open.
    Lambda bodies and generator-expression bodies/inner clauses are skipped at
    creation; lambda defaults and a generator's outermost iterable remain eager.
    Immediate invocation/consumption (a called lambda, ``next()``, or a loop) is
    not modeled; it needs context-sensitive execution modeling, and production
    has no exit-stack use.
    """

    def registers(current: ast.AST) -> frozenset[str]:
        if current is not node and isinstance(current, ast.stmt):
            return frozenset()
        if isinstance(current, ast.Lambda):
            return frozenset().union(
                *(
                    registers(default)
                    for default in (*current.args.defaults, *current.args.kw_defaults)
                    if default is not None
                )
            )
        if isinstance(current, ast.GeneratorExp):
            return registers(current.generators[0].iter)
        if (
            isinstance(current, ast.Call)
            and call_tail(current.func) in {"enter_context", "enter_async_context"}
            and any(
                isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Attribute)
                and argument.func.attr in {"begin", "begin_nested"}
                for argument in current.args
            )
        ):
            owner = _dotted_name(current.func.value) if isinstance(current.func, ast.Attribute) else None
            return frozenset({owner or "<unknown-exit-stack>"})
        return frozenset().union(*(registers(child) for child in ast.iter_child_nodes(current)))

    return registers(node)


def _opens_exit_stack(item: ast.withitem) -> bool:
    context = item.context_expr
    return isinstance(context, ast.Call) and call_tail(context.func) in {"ExitStack", "AsyncExitStack"}


@dataclass(frozen=True, slots=True)
class _EffectState:
    stack_transactions: frozenset[ast.withitem | None]
    stack_bindings: dict[str, frozenset[ast.withitem | None]]


def _join_effect_states(*states: _EffectState) -> _EffectState:
    names = {name for state in states for name in state.stack_bindings}
    return _EffectState(
        stack_transactions=frozenset(stack for state in states for stack in state.stack_transactions),
        stack_bindings={
            name: frozenset(stack for state in states for stack in state.stack_bindings.get(name, frozenset()))
            for name in names
        },
    )


def _bind_stack_name(
    state: _EffectState,
    target: ast.expr,
    stacks: frozenset[ast.withitem | None],
) -> _EffectState:
    name = _dotted_name(target)
    if name is None:
        return state
    bindings = dict(state.stack_bindings)
    bindings[name] = stacks
    return _EffectState(state.stack_transactions, bindings)


def _bound_stacks(state: _EffectState, value: ast.expr) -> frozenset[ast.withitem | None]:
    name = _dotted_name(value)
    return state.stack_bindings.get(name, frozenset({None})) if name is not None else frozenset({None})


def _register_stack_transactions(state: _EffectState, receivers: frozenset[str]) -> _EffectState:
    if not receivers:
        return state
    registered = frozenset(
        stack for receiver in receivers for stack in state.stack_bindings.get(receiver, frozenset({None}))
    )
    return _EffectState(state.stack_transactions | registered, state.stack_bindings)


def effects_inside_transactions_in_tree(tree: ast.Module, module: str) -> list[str]:
    """Effect calls nested inside a ``begin()``/``begin_nested()`` transaction in *tree*.

    Descends explicitly rather than through ``ast.walk`` because the property is
    about nesting: the ``with`` header itself is evaluated before the block is
    entered, while a call in the body -- or in a function *defined* in the body --
    runs with the transaction open.

    Two ways in, matching ``begin_owner_findings``: a ``with``/``async with`` item,
    and a ``begin()`` registered on an exit stack, which opens the transaction for
    the remainder of the enclosing block rather than for a nested one. Statement
    lists are therefore walked in order, with the flag flipping *after* the
    registering statement -- the registration's own arguments are evaluated before
    the transaction exists. Eager call inputs propagate state, but lambda bodies
    and generator-expression bodies/inner clauses are skipped at creation;
    lambda defaults and a generator's outermost iterable do propagate. Immediate
    invocation/consumption is not modeled; it needs context-sensitive execution
    modeling, and production has no exit-stack use.
    Branch states conservatively join both active transactions and possible
    name-to-stack bindings, so mutually exclusive runtime paths may still produce
    a finding.

    Direct ``begin()`` contexts are scoped to their ``with`` block. Exit-stack
    registrations are tracked by the identity of the ``with`` item that created
    the stack; plain name/attribute assignments carry that identity to aliases.
    Rebinding a name therefore cannot close an older stack, while any alias
    registration is cleared when that stack unwinds.
    """
    findings: list[str] = []
    appium = _uses_appium_direct(tree)

    def visit_body(
        statements: list[ast.stmt],
        state: _EffectState,
        scoped_transaction: bool = False,
    ) -> _EffectState:
        for statement in statements:
            state = visit(statement, state, scoped_transaction)
        return state

    def visit(
        node: ast.AST,
        state: _EffectState,
        scoped_transaction: bool = False,
    ) -> _EffectState:
        if isinstance(node, ast.Call):
            for child in ast.iter_child_nodes(node):
                state = visit(child, state, scoped_transaction)
            name = _effect_call_name(node, module_uses_appium_direct=appium)
            if name is not None and (state.stack_transactions or scoped_transaction):
                findings.append(f"{module}:{node.lineno} {name}()")
            return _register_stack_transactions(state, _registers_transaction_on_a_stack(node))
        if isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    state = visit(default, state, scoped_transaction)
            return state
        if isinstance(node, ast.GeneratorExp):
            return visit(node.generators[0].iter, state, scoped_transaction)
        if isinstance(node, ast.Assign):
            state = visit(node.value, state, scoped_transaction)
            stacks = _bound_stacks(state, node.value)
            for target in node.targets:
                state = _bind_stack_name(state, target, stacks)
            return state
        if isinstance(node, ast.With | ast.AsyncWith):
            direct_transaction = scoped_transaction
            closing_stacks: set[ast.withitem] = set()
            for item in node.items:
                state = visit(item.context_expr, state, direct_transaction)
                if _opens_exit_stack(item):
                    closing_stacks.add(item)
                    if item.optional_vars is not None:
                        state = _bind_stack_name(state, item.optional_vars, frozenset({item}))
                direct_transaction = direct_transaction or _opens_transaction(item)
            state = visit_body(node.body, state, direct_transaction)
            return _EffectState(state.stack_transactions - closing_stacks, state.stack_bindings)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            visit_body(node.body, state, scoped_transaction)
            return state
        if isinstance(node, ast.If | ast.For | ast.AsyncFor | ast.While):
            if isinstance(node, ast.If | ast.While):
                after_header = visit(node.test, state, scoped_transaction)
            else:
                after_header = visit(node.iter, state, scoped_transaction)
            body_state = visit_body(node.body, after_header, scoped_transaction)
            else_state = visit_body(node.orelse, after_header, scoped_transaction)
            return _join_effect_states(body_state, else_state)
        if isinstance(node, ast.Try | ast.TryStar):
            body_state = visit_body(node.body, state, scoped_transaction)
            handler_states: list[_EffectState] = []
            for handler in node.handlers:
                handler_state = body_state
                if handler.type is not None:
                    handler_state = visit(handler.type, handler_state, scoped_transaction)
                handler_states.append(visit_body(handler.body, handler_state, scoped_transaction))
            else_state = visit_body(node.orelse, body_state, scoped_transaction)
            return visit_body(
                node.finalbody,
                _join_effect_states(body_state, *handler_states, else_state),
                scoped_transaction,
            )
        if isinstance(node, ast.Match):
            subject_state = visit(node.subject, state, scoped_transaction)
            case_states: list[_EffectState] = []
            for case in node.cases:
                case_state = subject_state
                if case.guard is not None:
                    case_state = visit(case.guard, case_state, scoped_transaction)
                case_states.append(visit_body(case.body, case_state, scoped_transaction))
            return _join_effect_states(subject_state, *case_states)
        for child in ast.iter_child_nodes(node):
            state = visit(child, state, scoped_transaction)
        return _register_stack_transactions(
            state,
            _registers_transaction_on_a_stack(node) if isinstance(node, ast.stmt) else frozenset(),
        )

    visit(tree, _EffectState(frozenset(), {}))
    return findings


def effects_inside_transactions() -> list[str]:
    findings: list[str] = []
    for path in PRODUCTION:
        findings.extend(effects_inside_transactions_in_tree(parse_module(path), relative_module(path)))
    return findings


def _inventory(findings: list[tuple[str, str, int]]) -> str:
    return "\n".join(f'    ("{module}", "{owner}"),  # line {lineno}' for module, owner, lineno in findings)


def _drift_message(label: str, discovered: set[tuple[str, str]], registered: set[tuple[str, str]]) -> str:
    return (
        f"{label} drifted.\n"
        f"  unregistered owners: {sorted(discovered - registered)}\n"
        f"  registered but gone: {sorted(registered - discovered)}\n"
    )


def test_production_scan_is_not_empty() -> None:
    """A broken glob would make every assertion below vacuously true."""
    assert len(PRODUCTION) > 100, f"expected the whole app package, found {len(PRODUCTION)} modules"


def test_no_module_takes_direct_transaction_control() -> None:
    """Zero allowlist, of any shape. Every boundary in ``app/`` is a ``begin()``
    context with a named owner in ``BEGIN_OWNER_REGISTRY``."""
    discovered = transaction_control_owners()
    assert discovered == [], (
        "a module under app/ calls .commit() or .rollback() directly. Give the caller a "
        "session_factory.begin() boundary and register it in BEGIN_OWNER_REGISTRY:\n"
        f"{_inventory(discovered)}"
    )


def test_no_function_takes_a_transaction_control_argument() -> None:
    findings = transaction_control_arguments()
    assert findings == [], (
        "a function that takes its transaction decision as an argument is neither a command nor "
        f"transaction-local: {findings}"
    )


def test_begin_nested_is_confined_to_the_allowlist() -> None:
    discovered = begin_nested_owners()
    keys = {(module, owner) for module, owner, _lineno in discovered}
    assert keys == set(BEGIN_NESTED_ALLOWLIST), (
        _drift_message("BEGIN_NESTED_ALLOWLIST", keys, set(BEGIN_NESTED_ALLOWLIST))
        + f"full inventory:\n{_inventory(discovered)}"
    )


def test_every_begin_context_has_a_named_owner() -> None:
    discovered = begin_owners()
    keys = {(module, owner) for module, owner, _lineno in discovered}
    registered = {owner.key for owner in BEGIN_OWNER_REGISTRY}
    assert keys == registered, (
        _drift_message("BEGIN_OWNER_REGISTRY", keys, registered) + f"full inventory:\n{_inventory(discovered)}"
    )


def test_begin_detection_sees_both_context_shapes() -> None:
    """An exit-stack ``begin()`` opens the same transaction an ``async with`` does.

    Reproduced during Phase 10's review: the ``With``/``AsyncWith``-only scan
    missed ``stack.enter_async_context(db.begin())`` entirely, so a boundary
    entered that way would have had no registry entry and nothing would fail.
    """
    source = """
async def outer(db, stack):
    async with db.begin():
        pass
    await stack.enter_async_context(db.begin())
"""
    owners = begin_owner_findings(ast.parse(source), "synthetic.py")
    assert [owner for _module, owner, _lineno in owners] == ["outer", "outer"]


def test_the_effect_check_sees_a_transaction_opened_through_an_exit_stack() -> None:
    """An exit-stack ``begin()`` holds the same row locks an ``async with`` does.

    ``begin_owners`` was widened to see this shape; the effect check was not, so
    an agent dial inside a stack-opened transaction escaped the guard whose whole
    purpose is stopping a remote call from running with a row lock held. No
    module under ``app/`` uses an exit stack today, so this is the only place the
    widened detector can be falsified.
    """
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await stack.enter_async_context(db.begin())\n"
        "        await agent_health(ip, port)\n"
    )
    findings = effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py")
    assert findings == ["synthetic.py:4 agent_health()"]


def test_the_effect_check_does_not_flag_an_effect_before_the_stack_registration() -> None:
    """Order matters, and a stack with no ``begin()`` opens nothing.

    The registration call is evaluated before the transaction exists -- the same
    reasoning that makes a ``with`` header's own expressions out of scope -- so an
    effect above it is not inside anything, and a stack that never receives a
    ``begin()`` must not make its whole body look transactional.
    """
    before = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await agent_health(ip, port)\n"
        "        await stack.enter_async_context(db.begin())\n"
    )
    unrelated = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await stack.enter_async_context(db.stream())\n"
        "        await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(before), "synthetic.py") == []
    assert effects_inside_transactions_in_tree(ast.parse(unrelated), "synthetic.py") == []


def test_the_effect_check_tracks_a_stack_registration_within_an_if_body() -> None:
    """A registration opens the rest of its own compound statement body."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        if True:\n"
        "            await stack.enter_async_context(db.begin())\n"
        "            await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:5 agent_health()"]


def test_the_effect_check_ignores_a_stack_registration_inside_a_nested_definition() -> None:
    """Defining a function does not execute its exit-stack registration."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        async def register():\n"
        "            await stack.enter_async_context(db.begin())\n"
        "        await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == []


def test_the_effect_check_scans_compound_headers_inside_a_transaction() -> None:
    """A compound header is evaluated while its enclosing transaction is open."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with db.begin():\n"
        "        if await agent_health(ip, port):\n"
        "            pass\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:3 agent_health()"]


def test_the_effect_check_keeps_a_branch_registered_transaction_open_afterwards() -> None:
    """An exit-stack transaction can outlive the branch that registered it."""
    source = (
        "async def outer(db, ip, port, ready):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        if ready:\n"
        "            await stack.enter_async_context(db.begin())\n"
        "        await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:5 agent_health()"]


def test_the_effect_check_visits_else_after_a_transactional_if_branch() -> None:
    """Joining branch states must not short-circuit the second branch's scan."""
    source = (
        "async def outer(db, ip, port, ready):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        if ready:\n"
        "            await stack.enter_async_context(db.begin())\n"
        "        else:\n"
        "            async with db.begin():\n"
        "                await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:7 agent_health()"]


def test_the_effect_check_tracks_registration_order_within_a_compound_header() -> None:
    """Later terms see a transaction registered by an earlier header term."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        if (await stack.enter_async_context(db.begin())) and (await agent_health(ip, port)):\n"
        "            pass\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:3 agent_health()"]


def test_the_effect_check_tracks_registration_order_across_with_items() -> None:
    """Each ``with`` item is evaluated after the transaction state from prior items."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with (\n"
        "        AsyncExitStack() as stack,\n"
        "        await stack.enter_async_context(db.begin()),\n"
        "        await agent_health(ip, port),\n"
        "    ):\n"
        "        pass\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:5 agent_health()"]


def test_the_effect_check_keeps_a_stack_registration_from_a_nested_with_header() -> None:
    """A nested context ends before a transaction registered on the outer stack."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        async with passthrough(await stack.enter_async_context(db.begin())):\n"
        "            pass\n"
        "        await agent_health(ip, port)\n"
        "    await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:5 agent_health()"]


def test_the_effect_check_keeps_a_stack_registration_from_a_nested_with_body() -> None:
    """A registration in a nested body remains active until the outer stack exits."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        async with passthrough():\n"
        "            await stack.enter_async_context(db.begin())\n"
        "        await agent_health(ip, port)\n"
        "    await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:5 agent_health()"]


def test_the_effect_check_keeps_an_outer_stack_registration_past_an_inner_stack() -> None:
    """An inner exit stack must not clear a transaction owned by the outer stack."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as outer_stack:\n"
        "        async with AsyncExitStack():\n"
        "            await outer_stack.enter_async_context(db.begin())\n"
        "        await agent_health(ip, port)\n"
        "    await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:5 agent_health()"]


def test_the_effect_check_distinguishes_nested_stacks_with_the_same_name() -> None:
    """Rebinding a name must not collapse the two stack lifetimes."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await stack.enter_async_context(db.begin())\n"
        "        async with AsyncExitStack() as stack:\n"
        "            pass\n"
        "        await agent_health(ip, port)\n"
        "    await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:6 agent_health()"]


def test_the_effect_check_closes_a_transaction_registered_through_a_stack_alias() -> None:
    """An alias identifies the stack whose exit closes its transaction."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        alias = stack\n"
        "        await alias.enter_async_context(db.begin())\n"
        "        await agent_health(ip, port)\n"
        "    await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:5 agent_health()"]


def test_the_effect_check_keeps_a_branch_transaction_past_a_same_name_nested_stack() -> None:
    """A branch join must retain identity across a later name shadow."""
    source = (
        "async def outer(db, ip, port, ready):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        if ready:\n"
        "            await stack.enter_async_context(db.begin())\n"
        "        async with AsyncExitStack() as stack:\n"
        "            pass\n"
        "        await agent_health(ip, port)\n"
        "    await agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:7 agent_health()"]


def test_the_effect_check_sees_a_transaction_registered_by_an_effect_argument() -> None:
    """An effect runs after its arguments have been evaluated."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        if await agent_health(await stack.enter_async_context(db.begin())):\n"
        "            pass\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:3 agent_health()"]


def test_the_effect_check_sees_a_transaction_registered_by_its_callable() -> None:
    """An effect runs after its callable expression has been evaluated."""
    source = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await (await stack.enter_async_context(db.begin())).agent_health(ip, port)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(source), "synthetic.py") == ["synthetic.py:3 agent_health()"]


def test_the_effect_check_does_not_evaluate_a_lambda_body() -> None:
    """Lambda bodies are skipped at creation; defaults are evaluated then.

    Immediate invocation is not modeled; it needs context-sensitive execution
    modeling, and production has no exit-stack use.
    """
    deferred = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await agent_health(lambda: stack.enter_async_context(db.begin()))\n"
    )
    eager_default = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await agent_health(lambda transaction=stack.enter_context(db.begin()): transaction)\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(deferred), "synthetic.py") == []
    assert effects_inside_transactions_in_tree(ast.parse(eager_default), "synthetic.py") == [
        "synthetic.py:3 agent_health()"
    ]


def test_the_effect_check_only_evaluates_a_generator_expressions_outer_iterable() -> None:
    """Generator bodies/inner clauses are skipped at creation; the outer iterable is eager.

    Immediate consumption by ``next()`` or a loop is not modeled; it needs
    context-sensitive execution modeling, and production has no exit-stack use.
    """
    deferred_body = (
        "async def outer(db, ip, port, values):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await agent_health(stack.enter_async_context(db.begin()) for value in values)\n"
    )
    deferred_clause = (
        "async def outer(db, ip, port, values):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await agent_health(value for value in values if stack.enter_async_context(db.begin()))\n"
    )
    eager_outer_iterable = (
        "async def outer(db, ip, port):\n"
        "    async with AsyncExitStack() as stack:\n"
        "        await agent_health(value for value in [await stack.enter_async_context(db.begin())])\n"
    )
    assert effects_inside_transactions_in_tree(ast.parse(deferred_body), "synthetic.py") == []
    assert effects_inside_transactions_in_tree(ast.parse(deferred_clause), "synthetic.py") == []
    assert effects_inside_transactions_in_tree(ast.parse(eager_outer_iterable), "synthetic.py") == [
        "synthetic.py:3 agent_health()"
    ]


def test_begin_owner_kinds_are_command_or_infrastructure() -> None:
    kinds = {owner.kind for owner in BEGIN_OWNER_REGISTRY}
    assert kinds <= {"command", "infrastructure"}, f"unclassifiable begin() owner kinds: {sorted(kinds)}"


def test_begin_owner_registry_has_one_entry_per_owner() -> None:
    """A duplicated key with two kinds would let both classifications pass."""
    keys = [owner.key for owner in BEGIN_OWNER_REGISTRY]
    assert len(keys) == len(set(keys)), "BEGIN_OWNER_REGISTRY has a key registered under two kinds"


def test_every_effect_entry_point_is_registered() -> None:
    discovered = effect_owners()
    keys = {(module, owner) for module, owner, _lineno in discovered}
    assert keys == set(EFFECT_ENTRY_POINTS), (
        _drift_message("EFFECT_ENTRY_POINTS", keys, set(EFFECT_ENTRY_POINTS))
        + f"full inventory:\n{_inventory(discovered)}"
    )


def test_no_effect_runs_inside_a_transaction_block() -> None:
    """Name-set-based, not construct-based: this sees only the tails matched by
    AGENT_EFFECT_NAMES / APPIUM_DIRECT_NAMES / SUBPROCESS_NAMES / FILESYSTEM_NAMES
    (via ``_effect_call_name``), so a raw ``await client.post(...)`` inside a
    ``begin()`` block would not be caught here. That is fine today because raw
    ``httpx`` calls are confined to ``app/agent_comm/`` and
    ``app/grid/appium_direct.py``, both of which route through the named tails
    above -- but that confinement is no longer an observation: it is pinned by
    ``tests/contracts/test_raw_http_client_confinement.py``, which fails if any
    module outside those two locations constructs an HTTP client. The scan is
    also lexical -- an effect one call frame below a ``begin()`` block is
    invisible here, which is what the runtime-backed entries in
    ``REMOTE_EFFECT_OWNER_REGISTRY`` exist to cover. The transaction detector
    recognizes the same two syntactic ``begin()`` shapes as ``begin_owners``. For
    a directly constructed exit-stack ``with`` item, it distinguishes each stack
    lifetime and follows plain name/attribute aliases; an unresolved receiver is
    conservatively left active. It still skips a registration in a
    lambda/generator body at creation even when immediate invocation/consumption
    later runs it. Recognizing that execution needs context-sensitive modeling;
    production has no exit-stack use."""
    findings = effects_inside_transactions()
    assert findings == [], (
        "one of this repository's known effect entry points (see AGENT_EFFECT_NAMES / APPIUM_DIRECT_NAMES / "
        "SUBPROCESS_NAMES / FILESYSTEM_NAMES above) must not sit inside a begin() block: the transaction (and any "
        "row lock it holds) would stay open across it. Copy immutable scalars out, let the transaction end, then "
        f"act. Found: {findings}"
    )


def test_remote_effect_owners_are_registered_effect_entry_points() -> None:
    """A runtime-backed entry cannot outlive the code that justified it.

    Subset, not equality — deliberately, and uniquely in this file. Equality is
    impossible here: ``EFFECT_ENTRY_POINTS`` names every lexical effect entry
    point, and only a minority of them have a runtime no-active-transaction test.
    ``test_remote_effect_owner_registry_never_shrinks`` is the other half: this
    one stops an entry outliving its effect, that one stops the set emptying.
    """
    keys = {owner.key for owner in REMOTE_EFFECT_OWNER_REGISTRY}
    orphans = sorted(keys - set(EFFECT_ENTRY_POINTS))
    assert orphans == [], f"REMOTE_EFFECT_OWNER_REGISTRY names owners that make no effect call: {orphans}"


def test_remote_effect_owner_registry_never_shrinks() -> None:
    assert len(REMOTE_EFFECT_OWNER_REGISTRY) >= REMOTE_EFFECT_OWNER_FLOOR, (
        f"REMOTE_EFFECT_OWNER_REGISTRY dropped to {len(REMOTE_EFFECT_OWNER_REGISTRY)} entries, below the "
        f"floor of {REMOTE_EFFECT_OWNER_FLOOR}. A runtime-backed no-active-transaction proof was deleted "
        "without the code that justified it being deleted — restore it, or lower the floor in the same "
        "commit that removes the effect."
    )


def test_remote_effect_owner_tests_exist() -> None:
    """Every named no-active-transaction test must resolve to a real test function."""
    missing: list[str] = []
    for owner in sorted(REMOTE_EFFECT_OWNER_REGISTRY, key=lambda entry: entry.test_nodeid):
        relative, _, function = owner.test_nodeid.partition("::")
        path = BACKEND_ROOT / relative
        if not path.is_file():
            missing.append(f"{owner.test_nodeid} (no such file)")
            continue
        defined = {
            node.name
            for node in ast.walk(parse_module(path))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        if function not in defined:
            missing.append(f"{owner.test_nodeid} (no such test)")
    assert missing == [], f"REMOTE_EFFECT_OWNER_REGISTRY names tests that do not exist: {missing}"
