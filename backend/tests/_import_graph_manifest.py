"""Migration-aware import-graph manifest.

Grows over the backend domain-layout refactor. Each new phase appends
its domain to ``MIGRATED_DOMAINS`` and removes shims it deletes from
``LEGACY_SHIM_FILES``. Phase 16 cleanup deletes this file entirely.
"""

from __future__ import annotations

# Domains whose code now lives under ``app/<domain>/`` and which the
# import-graph guard enforces deep-import rules against.
MIGRATED_DOMAINS: frozenset[str] = frozenset(
    {
        "auth",
        "analytics",
        "settings",
        "webhooks",
        "events",
        "jobs",
        "grid",
        "plugins",
        "agent_comm",
    }
)

# Files at original paths that re-export from their new home in
# ``app/core/`` or ``app/<domain>/``. The graph guard exempts these
# from all rules so they can keep their cross-cutting
# ``from app.X import ...`` lines until every caller migrates.
# Phase 16 deletes all shims.
LEGACY_SHIM_FILES: frozenset[str] = frozenset(
    {
        # Phase 0a
        "app/metrics.py",
        # Phase 0b
        "app/config.py",
        "app/database.py",
        "app/type_defs.py",
        "app/shutdown.py",
        "app/metrics_recorders.py",
        "app/services/cursor_pagination.py",
        "app/services/csv_export.py",
        # Phase 1
        "app/dependencies.py",
        "app/middleware.py",
        "app/routers/auth.py",
        "app/routers/analytics.py",
        "app/routers/settings.py",
        "app/routers/webhooks.py",
        "app/routers/events.py",
        "app/routers/grid.py",
        "app/routers/plugins.py",
        "app/schemas/auth.py",
        "app/schemas/analytics.py",
        "app/schemas/setting.py",
        "app/schemas/webhook.py",
        "app/schemas/event.py",
        "app/schemas/event_catalog.py",
        "app/schemas/grid.py",
        "app/schemas/health.py",
        "app/schemas/plugin.py",
        "app/security/__init__.py",
        "app/security/dependencies.py",
        "app/services/auth.py",
        "app/services/auth_dependencies.py",
        "app/services/analytics_service.py",
        "app/models/analytics_capacity_snapshot.py",
        "app/services/settings_registry.py",
        "app/services/settings_service.py",
        "app/services/config_service.py",
        "app/models/setting.py",
        "app/models/config_audit_log.py",
        "app/services/webhook_dispatcher.py",
        "app/services/webhook_service.py",
        "app/models/webhook.py",
        "app/models/webhook_delivery.py",
        "app/services/event_bus.py",
        "app/services/event_catalog.py",
        "app/services/system_event_service.py",
        "app/models/system_event.py",
        "app/services/job_queue.py",
        "app/services/job_kind_constants.py",
        "app/services/job_status_constants.py",
        "app/models/job.py",
        "app/services/grid_service.py",
        "app/services/plugin_service.py",
        "app/models/appium_plugin.py",
        "app/agent_client.py",
        "app/services/agent_http_pool.py",
        "app/services/agent_circuit_breaker.py",
        "app/services/agent_operations.py",
        "app/services/agent_error_codes.py",
        "app/services/agent_probe_result.py",
        "app/services/agent_reconfigure_delivery.py",
        "app/services/agent_snapshot.py",
        "app/models/agent_reconfigure_outbox.py",
        # Phase 1 — ``app/core/config.py`` carries auth-forwarding
        # properties that import ``app.auth.auth_settings`` at the top
        # of the module. This breaks the "core-purity" rule for the
        # duration of the auth forwarders. Phase 16 deletes the
        # forwarders along with this exemption.
        "app/core/config.py",
    }
)
