"""Single source for the decision-fact model/column tables.

Consumed by two contracts: the lexical scan in
``test_no_direct_device_state_writes.py`` (string-keyed, AST-side) and the
runtime device-lock guard in ``device_lock_guard.py`` (class-keyed, ORM-side).
Change a table here and both layers move together.
"""

from __future__ import annotations

DECISION_FACT_MODELS = {
    "DeviceIntent": "device_intent",
    "Session": "live_session",
    "DeviceReservation": "device_reservation",
    "DeviceRemediationLogEntry": "remediation_log_entry",
}
# The columns whose change makes a bulk UPDATE a decision-fact write.
DECISION_COLUMNS: dict[str, frozenset[str]] = {
    "device_intent": frozenset({"payload", "expires_at"}),
    "live_session": frozenset({"status", "ended_at"}),
    "device_reservation": frozenset({"released_at", "excluded"}),
    "remediation_log_entry": frozenset({"backoff_until"}),
}


def watched_orm_columns() -> dict[type, frozenset[str]]:
    """Class-keyed watch map for the runtime guard.

    Imports app models lazily so AST-only consumers of this module never pay
    for (or fail on) an app import at collection time.
    """
    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device
    from app.devices.models.intent import DeviceIntent
    from app.devices.models.remediation_log import DeviceRemediationLogEntry
    from app.devices.models.reservation import DeviceReservation
    from app.sessions.models import Session

    return {
        DeviceIntent: DECISION_COLUMNS["device_intent"],
        Session: DECISION_COLUMNS["live_session"],
        DeviceReservation: DECISION_COLUMNS["device_reservation"],
        DeviceRemediationLogEntry: DECISION_COLUMNS["remediation_log_entry"],
        # The two Device attribute facts and the three AppiumNode desired-state
        # columns; observation columns are deliberately absent (see the spec's
        # non-goals).
        Device: frozenset({"failure_episode_id", "operational_state_last_emitted"}),
        AppiumNode: frozenset({"desired_state", "desired_port", "restart_requested_at"}),
    }


def fact_for_model() -> dict[type, str]:
    """Model class -> fact name, for guard error messages and mode checks."""
    mapping = watched_orm_columns()
    by_name = {model.__name__: model for model in mapping}
    return {by_name[name]: fact for name, fact in DECISION_FACT_MODELS.items()}
