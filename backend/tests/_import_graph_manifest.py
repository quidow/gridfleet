"""Migration-aware import-graph manifest.

Grows over the backend domain-layout refactor. Each new phase appends
its domain to ``MIGRATED_DOMAINS`` and removes shims it deletes from
``LEGACY_SHIM_FILES``. Phase 16 cleanup deletes this file entirely.
"""

from __future__ import annotations

# Domains whose code now lives under ``app/<domain>/`` and which the
# import-graph guard enforces deep-import rules against. Empty in
# Phase 0b — domain migrations begin at Phase 1.
MIGRATED_DOMAINS: frozenset[str] = frozenset()

# Files at original paths that re-export from their new home in
# ``app/core/`` or ``app/<domain>/``. The graph guard exempts these
# from all rules so they can keep their cross-cutting
# ``from app.X import ...`` lines until every caller migrates.
# Phase 16 deletes all shims.
LEGACY_SHIM_FILES: frozenset[str] = frozenset(
    {
        "app/config.py",
        "app/database.py",
        "app/type_defs.py",
        "app/shutdown.py",
        "app/metrics_recorders.py",
        "app/services/cursor_pagination.py",
        "app/services/csv_export.py",
        # Phase 0a shim — the legacy aggregator and re-exports live
        # here until the contributing domains migrate.
        "app/metrics.py",
    }
)
