"""Python GC tuning for the long-running backend process.

CPython's default gen-0 threshold (700 allocations) is tuned for short-lived
scripts; a busy async control plane allocates heavily, which drives frequent
collections all the way up to full gen-2 sweeps over a large, mostly
long-lived heap. Freezing the post-startup heap removes long-lived objects
(app, routes, settings, pools) from every sweep; raising gen-0 cuts collection
frequency proportionally. Gen-1/gen-2 multipliers keep their defaults so the
generational ratios are unchanged.
"""

from __future__ import annotations

import gc

GEN0_THRESHOLD = 5000


def tune_after_startup() -> None:
    """Call once at the end of lifespan startup, before serving traffic."""
    gc.collect()
    gc.freeze()
    threshold = gc.get_threshold()
    gc.set_threshold(GEN0_THRESHOLD, threshold[1], threshold[2])
