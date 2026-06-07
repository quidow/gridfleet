"""Vulture whitelist for framework-invoked patterns.

Additions require justification in the PR description.
"""

# SQLAlchemy attribute-event listener callback parameters
# Signature: (target, value, oldvalue, initiator) — all four required by SA.
# Used in app/devices/services/state_write_guard.py::_make_listener
oldvalue  # noqa: F821
initiator  # noqa: F821

# BackgroundLoop._on_cycle_end framework-hook parameter consumed by subclasses.
elapsed_seconds  # noqa: F821
