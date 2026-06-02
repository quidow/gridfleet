"""Lifecycle domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.incidents import LifecycleIncidentService
    from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.lifecycle.services.recovery_job import RecoveryJobService


@dataclass(frozen=True, slots=True)
class LifecycleServices:
    policy: LifecyclePolicyService
    actions: LifecyclePolicyActionsService
    operator_node: OperatorNodeLifecycleService
    incidents: LifecycleIncidentService
    recovery: RecoveryJobService
