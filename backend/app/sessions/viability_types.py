"""Shared types for session viability probe results.

`SessionViabilityCheckedBy` is the single source of truth for who triggered a
viability probe. Use it on every writer (`record_session_viability_result`,
`run_session_viability_probe`, `_write_session_viability`) and on the public
`SessionViabilityRead` response schema so reader and writer cannot drift.

The probe exception pair lives here so row-claim code (`service_probes`) can
raise it without importing the service module.
"""

from __future__ import annotations

from enum import StrEnum


class SessionViabilityCheckedBy(StrEnum):
    scheduled = "scheduled"
    manual = "manual"
    recovery = "recovery"
    verification = "verification"


class SessionViabilityProbeInProgressError(ValueError):
    """Raised when a viability probe cannot start because one is already in flight.

    Subclasses ``ValueError`` so manual HTTP callers keep surfacing 409 (control.py),
    while the distinct type lets the lifecycle recovery loop tell a claim *collision*
    (another probe's live Session row already claims the device — e.g. an active
    verification probe) apart from a probe *failure*. A collision says nothing about
    device health, so recovery skips it instead of counting a failed attempt that
    would feed backoff/shelving.
    """


class SessionViabilityProbeNotPermittedError(ValueError):
    """Raised when the device's current state does not permit a probe.

    Subclasses ``ValueError`` so manual HTTP callers keep surfacing 409 (control.py).
    The distinct type lets the lifecycle recovery loop treat a *gating* rejection
    (the device is no longer ``offline``/``verifying`` — e.g. ``busy``/``maintenance``,
    or its state changed concurrently between the pre-lock gate and the row lock) as a
    *skip* rather than a failed attempt. Like a probe collision, a gate rejection says
    nothing about device health, so counting it would feed backoff/shelving. Mirrors
    ``SessionViabilityProbeInProgressError``.
    """
