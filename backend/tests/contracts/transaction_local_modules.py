"""The transaction-local module registry.

Data only. The commit/rollback property this file once asserted per listed
module is now asserted for the whole of ``app/`` with no allowlist by
``tests/contracts/test_repository_transaction_boundaries.py``; duplicating it
here would give one fact two homes that can disagree. The tuple survives
because two other contracts read it as the transaction-locality registry:
``test_no_direct_device_state_writes.py``'s ``caller_locked`` proof mode and
``test_phase9_domain_command_boundaries.py``'s gate. It is named without a
``test_`` prefix because it collects no tests -- under one it read as a test module
that had silently stopped asserting anything.
"""

MIGRATED_TRANSACTION_LOCAL_MODULES = (
    "app/devices/services/intent.py",
    "app/devices/services/intent_reconciler.py",
    "app/devices/services/decision_snapshot.py",
    "app/devices/services/state.py",
    "app/grid/allocation.py",
    "app/sessions/service.py",
    "app/sessions/service_probes.py",
    "app/runs/service_reservation.py",
    "app/runs/service_lifecycle_release.py",
    "app/verification/services/preparation.py",
    "app/verification/services/execution.py",
    "app/core/db_retry.py",
    "app/grid/router_internal.py",
    "app/grid/session_create.py",
    "app/sessions/service_kill.py",
    "app/sessions/service_viability.py",
    "app/sessions/service_sync.py",
    "app/runs/service_allocator.py",
    "app/runs/service_lifecycle.py",
    "app/runs/service_lifecycle_failures.py",
    "app/runs/service_teardown.py",
    "app/verification/services/runner.py",
    "app/appium_nodes/services/heartbeat.py",
    "app/appium_nodes/services/host_sweep.py",
    "app/appium_nodes/services/reconciler.py",
    "app/appium_nodes/services/status_fold_loop.py",
    "app/devices/services/property_refresh.py",
    "app/hosts/router_agent.py",
    "app/hosts/service_resource_telemetry.py",
    "app/hosts/service_status_push.py",
    # Phase 9 task 1: device persistence, test-data, and config writers.
    "app/devices/services/write.py",
    "app/devices/services/service.py",
    "app/devices/services/test_data.py",
    "app/settings/service_config.py",
    # Phase 9 task 2: maintenance, bulk, and the three operator node actions.
    "app/devices/services/bulk.py",
    "app/devices/services/maintenance.py",
    "app/appium_nodes/services/reconciler_agent.py",
    "app/appium_nodes/routers/nodes.py",
    # Phase 9 task 3: host commands and the pack discovery effects.
    "app/hosts/router.py",
    "app/hosts/service.py",
    "app/packs/services/discovery.py",
    # Phase 9 task 4: the pack catalog reads and the pack lifecycle commands.
    "app/packs/routers/catalog.py",
    "app/packs/routers/uploads.py",
    "app/packs/services/lifecycle.py",
    "app/packs/services/service.py",
    # Phase 9 task 5: the settings mutations, the last of the phase's 16 files.
    "app/settings/service.py",
    # Phase 10 task 7: the inherited fold, lifecycle, and route boundaries. These
    # five were the last entries of the retired SANCTIONED_COMMIT_BOUNDARIES dict —
    # each carried a scheduler/job commit that its own caller now owns.
    "app/appium_nodes/services/node_health.py",
    "app/devices/services/connectivity.py",
    "app/lifecycle/services/actions.py",
    "app/lifecycle/services/policy.py",
    "app/devices/services/health.py",
    # Phase 10 task 7: the two routes and the outbox poller that owned a raw
    # commit/rollback on a session they did not open.
    "app/sessions/router.py",
    "app/devices/routers/control.py",
    "app/events/event_bus.py",
    # Phase 10 tasks 1-2 converted these but never listed them; they are clean.
    "app/core/janitor.py",
    "app/core/observability.py",
    "app/main.py",
    # Phase 10 task 8: the remediation-ladder appender. Already clean; listed so
    # DECISION_FACT_WRITERS can rely on it being pinned transaction-local, which
    # is what makes its ``caller_locked`` proof mode enforceable rather than
    # asserted (see tests/contracts/test_no_direct_device_state_writes.py).
    "app/lifecycle/services/remediation_log.py",
)
