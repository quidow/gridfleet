"""Shared types for session viability probe results.

`SessionViabilityCheckedBy` is the single source of truth for who triggered a
viability probe. Use it on every writer (`record_session_viability_result`,
`run_session_viability_probe`, `_write_session_viability`) and on the public
`SessionViabilityRead` response schema so reader and writer cannot drift.

The probe exception trio lives here so row-claim code (`service_probes`) can
raise it without importing the service module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid


class SessionViabilityCheckedBy(StrEnum):
    scheduled = "scheduled"
    manual = "manual"
    recovery = "recovery"
    verification = "verification"
    # Not a probe: the observation sweep's own direct evidence that a node's
    # Appium is unreachable (P1). Named separately so the device health panel
    # says where a failure came from.
    observation = "observation"


@dataclass(frozen=True, slots=True)
class NodeReachability:
    """One observation sweep's per-device enumeration verdict.

    ``observed`` is every device the sweep enumerated this tick (candidates with
    ``desired_state == running``); ``unreachable`` is the subset whose Appium
    never answered — a transport failure, not a refusal from a node that is alive
    but cannot enumerate. Lives here rather than in ``service_sync`` so the sweep
    that produces it and the viability service that consumes it share one
    definition without importing each other.
    """

    observed: tuple[uuid.UUID, ...]
    unreachable: frozenset[uuid.UUID]


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


class SessionViabilityReadinessLapsedError(ValueError):
    """Raised when the device is no longer ready for use at probe time.

    Subclasses ``ValueError`` so manual HTTP callers keep surfacing 409 (control.py)
    and the message stays the readiness detail string. The distinct type exists so
    the lifecycle recovery loop's skip catch can name all three precondition lapses
    instead of falling back to the base ``ValueError`` — which would silently swallow
    any unrelated ``ValueError`` raised deeper in the probe (capability derivation, a
    parse) and return ``skipped`` where ``failed`` is owed. Third of the trio with
    ``SessionViabilityProbeInProgressError`` and
    ``SessionViabilityProbeNotPermittedError``.
    """
