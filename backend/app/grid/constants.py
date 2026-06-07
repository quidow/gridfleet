"""Leaf constants shared across the grid package (imports nothing from it).

``RETRY_INTERVAL_SEC`` is the allocate long-poll re-attempt cadence. It used to
be duplicated in ``router_internal`` (the authoritative sleep) and
``allocation`` (which sizes the ticket-staleness window from it) because
importing one from the other would create an allocation<-router_internal
cycle; changing one copy silently desynced the other and with it the staleness
window (wave-5 review #15). Both now import from here.
"""

RETRY_INTERVAL_SEC = 1.0
