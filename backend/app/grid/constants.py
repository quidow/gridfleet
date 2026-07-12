"""Leaf constants shared across the grid package (imports nothing from it).

``RETRY_INTERVAL_SEC`` is the allocate long-poll re-attempt cadence. It used to
be duplicated in ``router_internal`` (the authoritative sleep) and
``allocation`` (which sizes the ticket-staleness window from it) because
importing one from the other would create an allocation<-router_internal
cycle; changing one copy silently desynced the other and with it the staleness
window (wave-5 review #15). Both now import from here.
"""

RETRY_INTERVAL_SEC = 1.0

# The create-session long-poll slice: how long POST /internal/grid/create-session holds a
# waiting request before returning "retry". Lives here (not router_internal)
# so app.settings.invariants can import it cycle-free. Ordered budgets built
# on it: grid.queue_timeout_sec must exceed it (settings invariant); the
# router's shared HTTP client timeout (40s, router/src/backend.rs) must exceed
# it (compile-time assert there). See the timeout-lattice table in
# docs/reference/architecture.md.
LONG_POLL_SEC: float = 25.0
