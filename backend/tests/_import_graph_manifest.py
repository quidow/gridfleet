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
        "app/schemas/auth.py",
        "app/security/__init__.py",
        "app/security/dependencies.py",
        "app/services/auth.py",
        "app/services/auth_dependencies.py",
        # Phase 1 — ``app/core/config.py`` carries auth-forwarding
        # properties that import ``app.auth.auth_settings`` at the top
        # of the module. This breaks the "core-purity" rule for the
        # duration of the auth forwarders. Phase 16 deletes the
        # forwarders along with this exemption.
        "app/core/config.py",
    }
)
