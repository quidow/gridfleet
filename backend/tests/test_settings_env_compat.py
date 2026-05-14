"""Per-domain BaseSettings env/field-name compat coverage.

Empty in Phase 0b. Phase 1 adds ``AuthConfig`` and the first test
that constructs it from both env vars and field-name kwargs to lock
``populate_by_name=True`` semantics. Every subsequent domain-config
phase appends a similar test pair here.
"""

from __future__ import annotations


def test_settings_env_compat_placeholder() -> None:
    # Real tests added in Phase 1+. This placeholder exists so the
    # filename appears in the suite and import-graph manifests can
    # refer to it from the first phase that ships a BaseSettings.
    assert True
