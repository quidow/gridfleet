"""Vulture whitelist for framework-invoked patterns.

Additions require justification in the PR description.
"""

# BackgroundLoop._on_cycle_end framework-hook parameter consumed by subclasses.
elapsed_seconds  # noqa: F821
